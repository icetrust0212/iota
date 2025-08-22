import asyncio
from common.models.miner_models import ChunkMetadata
from common.models.api_models import SubmittedWeightsAndOptimizerPresigned
from common.utils.partitions import MinerPartition
from loguru import logger
from miner.utils.utils import download_metadata
from subnet.utils.s3_torch import download_weights_or_optimizer_state
import torch

"""
4 Metadata chunks (at weight uploading)
XXXXXXXXXXXX| XXXXXXXXXXXX| XXXXXXXXXXXX| XXXXXXXXXXXX

6 Partitions (because between weight uploading and merging new miners registered, meaning we 'think' more partitions are needed)
XXXXXXXX|XXXXXXXX|XXXXXXXX|XXXXXXXX|XXXXXXXX|XXXXXXXX

-> We get the start & end indices from the metadata, hence
Partition 1: XXXXXXXXXXXX|
Partition 2: XXXXXXXXXXXX|
Partition 3: XXXXXXXXXXXX|
Partition 4: XXXXXXXXXXXX|
Partition 5: Invalid (No metadata chunk found)
Partition 6: Invalid (No metadata chunk found)

--> The total amonunt of data is still consistent and we can just discard the invalid partitions.
"""


async def get_chunk_metadata_for_all_partitions(
    submitted_weights_and_optimizer: SubmittedWeightsAndOptimizerPresigned, partitions: list[MinerPartition]
) -> dict[int, dict[str, ChunkMetadata]]:
    """
    Returns a dictionary as follows:
    {
        0: {  <- partition number
            "weights": ChunkMetadata(...),
            "optimizer_state": ChunkMetadata(...),
        },
        ...
        7: {  <- partition number
            "weights": ChunkMetadata(...),
            "optimizer_state": ChunkMetadata(...),
        },
    }
    """
    try:
        # Download both weight and optimizer state metadata
        weight_metadata: dict = await download_metadata(
            metadata_path=submitted_weights_and_optimizer.weight_metadata_path_presigned
        )
        optimizer_state_metadata: dict = await download_metadata(
            metadata_path=submitted_weights_and_optimizer.optimizer_state_metadata_path_presigned
        )
        if not weight_metadata or not optimizer_state_metadata:
            raise Exception(
                f"No weight or optimizer state metadata found | {submitted_weights_and_optimizer.weights_path_presigned}"
            )
        if (
            "weight" not in submitted_weights_and_optimizer.weight_metadata_path_presigned
            and "weight" not in submitted_weights_and_optimizer.weights_path_presigned
        ):
            raise Exception(
                f"Weight metadata path does not contain 'weight' | {submitted_weights_and_optimizer.weights_path_presigned}"
            )
        if (
            "optimizer_state" not in submitted_weights_and_optimizer.optimizer_state_metadata_path_presigned
            and "optimizer_state" not in submitted_weights_and_optimizer.optimizer_state_path_presigned
        ):
            raise Exception(
                f"Optimizer state metadata path does not contain 'optimizer_state' | {submitted_weights_and_optimizer.optimizer_state_path_presigned}"
            )

        metadata_infos: dict[int, dict] = {}
        for partition in partitions:
            try:
                chunk_number = partition.chunk_number

                # Create separate metadata info for weights and optimizer state
                weight_metadata_info = ChunkMetadata(
                    **weight_metadata["sections"][str(chunk_number)],
                    chunk_number=chunk_number,
                    weighting_factor=submitted_weights_and_optimizer.weighting_factor,
                    tensor_path=submitted_weights_and_optimizer.weights_path_presigned,
                    metadata_path=submitted_weights_and_optimizer.weight_metadata_path_presigned,
                    chunk_dtype=weight_metadata["tensor"]["dtype"].split(".")[-1],
                    data_type="weights",
                )

                optimizer_state_metadata_info = ChunkMetadata(
                    **optimizer_state_metadata["sections"][str(chunk_number)],
                    chunk_number=chunk_number,
                    weighting_factor=submitted_weights_and_optimizer.weighting_factor,
                    tensor_path=submitted_weights_and_optimizer.optimizer_state_path_presigned,
                    metadata_path=submitted_weights_and_optimizer.optimizer_state_metadata_path_presigned,
                    chunk_dtype=optimizer_state_metadata["tensor"]["dtype"].split(".")[-1],
                    data_type="optimizer_state",
                )

                metadata_infos[chunk_number] = {
                    "weights": weight_metadata_info,
                    "optimizer_state": optimizer_state_metadata_info,
                }
            except Exception as e:
                logger.error(f"Error getting chunk metadata for partition {partition.chunk_number}: {e}")
                continue

        return metadata_infos

    except Exception as e:
        logger.exception(
            f"Error getting chunk metadata for all partitions. This is likely due to bad metadata being uploaded but is not a fatal error.: {e}"
        )
        logger.error(f"Bad metadata; WEIGHTS: {weight_metadata} | OPTIMIZER STATE: {optimizer_state_metadata}")
        return None


async def download_partition(
    weight_metadata: ChunkMetadata, optimizer_metadata: ChunkMetadata
) -> tuple[torch.Tensor, torch.Tensor]:
    try:
        assert weight_metadata.data_type == "weights", "Weights metadata is not of type weights"
        assert (
            optimizer_metadata.data_type == "optimizer_state"
        ), "Optimizer state metadata is not of type optimizer_state"
        assert (
            weight_metadata.chunk_number == optimizer_metadata.chunk_number
        ), "Weights and optimizer state chunk numbers do not match"
        assert "weight" in weight_metadata.tensor_path, "Weights tensor path does not contain 'weight'"
        assert (
            "optimizer_state" in optimizer_metadata.tensor_path
        ), "Optimizer state tensor path does not contain 'optimizer_state'"
        assert "weight" in weight_metadata.metadata_path, "Weights metadata path does not contain 'weight'"
        assert (
            "optimizer_state" in optimizer_metadata.metadata_path
        ), "Optimizer state metadata path does not contain 'optimizer_state'"

        weights: torch.Tensor = await download_weights_or_optimizer_state(
            metadata_info=weight_metadata,
        )
        optimizer_state: torch.Tensor = await download_weights_or_optimizer_state(
            metadata_info=optimizer_metadata,
        )
    except Exception as e:
        logger.error(
            f"Error download partition with weight metadata: {weight_metadata} and otpimizer metadata: {optimizer_metadata}"
        )
        logger.exception(f"Error downloading weights and optimizer state: {e}")
        raise

    return weights, optimizer_state


def metadata_matches(meta1: dict[int, dict[str, ChunkMetadata]], meta2: dict[int, dict[str, ChunkMetadata]]) -> bool:
    """Check if two metadata dictionaries match."""
    # Check if the chunk numbers are the same
    if set(meta1.keys()) != set(meta2.keys()):
        return False

    # Check that all the start/end indices agree with each other
    for chunk_number in meta1.keys():
        for data_type in ["weights", "optimizer_state"]:
            m1, m2 = meta1[chunk_number][data_type], meta2[chunk_number][data_type]
            if not m1.compatible(m2):
                return False
    return True


async def filter_bad_metadata(
    partitions: list[MinerPartition],
    submitted_weights_and_optimizers: list[SubmittedWeightsAndOptimizerPresigned],
) -> dict[str, dict[int, dict[str, ChunkMetadata]]]:
    """Filter out packets with bad metadata and return valid metadata info objects.

    Returns: dictionary mapping weight path to dictionary mapping chunk number to dictionary mapping data type to ChunkMetadata

    Partitions: 0,1,2,3,4,5,6,7
    Weights that were uploaded: 0,1,2

    Each weight (0,1,2) comes with its own metadata on s3 containing the start and end indices for each partition (0,1,2,3,4,5,6,7)

    All these metadata objects SHOULD agree with each other, but because miners upload them, they might not.

    This function now takes the most 'common' metadata object and filters out the rest.

    So in our case, the return would look as follows:

    {
        {
            weight_0: {
                0: {
                    "weights": ChunkMetadata(...),
                    "optimizer_state": ChunkMetadata(...),
                },
                ...
                7: {
                    "weights": ChunkMetadata(...),
                    "optimizer_state": ChunkMetadata(...),
                }
            },
            ...,
            weight_2: {
                0: {
                    "weights": ChunkMetadata(...),
                    "optimizer_state": ChunkMetadata(...),
                },
                ...
                7: {
                    "weights": ChunkMetadata(...),
                    "optimizer_state": ChunkMetadata(...),
                }
            }
        }
    }
    """
    # Collect valid metadata from all packets
    valid_metadata: dict[str, dict[int, dict[str, ChunkMetadata]]] = {}
    results = await asyncio.gather(
        *[
            get_chunk_metadata_for_all_partitions(submitted_weights_and_optimizer=s, partitions=partitions)
            for s in submitted_weights_and_optimizers
        ]
    )
    valid_metadata = {
        s.weights_path_presigned: r
        for s, r in zip(submitted_weights_and_optimizers, results, strict=True)
        if r is not None
    }

    if not valid_metadata:
        logger.warning("No valid metadata found")
        return {}

    # Compare all metadata objects to each other and find the most common pattern
    agreement_counts: dict[str, int] = {}
    for path1, meta1 in valid_metadata.items():
        agreement_counts[path1] = sum(
            1 for path2, meta2 in valid_metadata.items() if path1 != path2 and metadata_matches(meta1, meta2)
        )

    # Keep only metadata that matches the most common pattern
    best_path = max(agreement_counts, key=agreement_counts.get)
    best_metadata = valid_metadata[best_path]

    filtered_metadata = {path: meta for path, meta in valid_metadata.items() if metadata_matches(best_metadata, meta)}

    filtered_count = len(valid_metadata) - len(filtered_metadata)
    if filtered_count > 0:
        logger.warning(f"Filtered out {filtered_count} packets due to metadata disagreements")

    return filtered_metadata
