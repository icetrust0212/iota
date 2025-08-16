import json
from datetime import datetime
from hashlib import sha256
from typing import Callable, Optional

import bittensor as bt
from common.utils.exceptions import NanInfWarning
import torch
from bittensor_wallet import Keypair
from bittensor_wallet.mock import get_mock_wallet
from common import settings as common_settings
from common.models.miner_models import MetadataInfo, MinerStatus
from common.utils.partitions import MinerPartition, format_chunk_data
from common.utils.s3_utils import download_file
from common.utils.shared_states import LayerPhase
from loguru import logger
from subnet.common_api_client import CommonAPIClient
from subnet.model.model_mixin import ModelManager
from subnet.utils.bt_utils import get_wallet
from subnet.utils.s3_torch import download_activation
from subnet.utils.vector_utils import (
    check_for_nans_and_infs,
    flatten_optimizer_state,
    reconstruct_optimizer_state,
)


class BaseNeuron:
    def __init__(self):
        super().__init__()
        self.wallet: bt.wallet | None = None
        self.layer: Optional[int] = None
        self.status: str = MinerStatus.IDLE.value
        self.registration_time: str = datetime.now().isoformat()
        self.merge_status: str = LayerPhase.TRAINING.value

        self.model_manager = ModelManager()

    def init_neuron(
        self, wallet_name: str = None, wallet_hotkey: str = None, mock: bool = False, wallet: bt.wallet | None = None
    ):
        if common_settings.BITTENSOR:
            if not wallet:
                self.create_wallet(wallet_name=wallet_name, wallet_hotkey=wallet_hotkey, mock=mock)
        else:
            coldkey = Keypair.create_from_seed(seed=sha256(wallet_hotkey.encode()).hexdigest())
            hotkey = Keypair.create_from_seed(seed=sha256(wallet_name.encode()).hexdigest())
            self.wallet = get_mock_wallet(hotkey=hotkey, coldkey=coldkey)

        self.hotkey = self.wallet.hotkey.ss58_address
        logger.info(f"Launching with hotkey: {self.hotkey}")

        return self

    async def _setup_local_model(
        self, model_weights: torch.Tensor, optimizer_state: dict, layer: int, device: str
    ) -> bool:
        try:
            logger.info(f"📥 Attempting to load model for layer {layer} for {self.hotkey[:8]}")
            await self.model_manager.initialize_model_manager(
                model_weights=model_weights,
                optimizer_state=optimizer_state,
                layer=layer,
                device=device,
                logger_attributes={
                    "hotkey": self.hotkey,
                },
            )

        except Exception as e:
            logger.error(f"❌ Error loading model for miner {self.hotkey[:8]}: {e}")
            return False
        return True

    def create_wallet(self, wallet_name: str, wallet_hotkey: str, mock: bool):
        self.wallet = get_wallet(
            wallet_name=wallet_name,
            wallet_hotkey=wallet_hotkey,
            mock=mock,
        )
        return self

    async def build_chunk_data(self, partition: MinerPartition):
        weight_metadata_bytes: bytes = await download_file(presigned_url=partition.weight_metadata_path)
        weight_metadata: dict = json.loads(weight_metadata_bytes)
        optimizer_state_metadata_bytes: bytes = await download_file(
            presigned_url=partition.optimizer_state_metadata_path
        )
        optimizer_state_metadata: dict = json.loads(optimizer_state_metadata_bytes)
        weight_metadata = MetadataInfo(
            start_idx=weight_metadata["sections"][str(partition.chunk_number)]["start_idx"],
            end_idx=weight_metadata["sections"][str(partition.chunk_number)]["end_idx"],
            start_byte=weight_metadata["sections"][str(partition.chunk_number)]["start_byte"],
            end_byte=weight_metadata["sections"][str(partition.chunk_number)]["end_byte"],
            chunk_dtype=weight_metadata["tensor"]["dtype"].split(".")[-1],
            chunk_number=partition.chunk_number,
        )
        optimizer_state_metadata = MetadataInfo(
            start_idx=optimizer_state_metadata["sections"][str(partition.chunk_number)]["start_idx"],
            end_idx=optimizer_state_metadata["sections"][str(partition.chunk_number)]["end_idx"],
            start_byte=optimizer_state_metadata["sections"][str(partition.chunk_number)]["start_byte"],
            end_byte=optimizer_state_metadata["sections"][str(partition.chunk_number)]["end_byte"],
            chunk_dtype=optimizer_state_metadata["tensor"]["dtype"].split(".")[-1],
            chunk_number=partition.chunk_number,
        )
        partition.weight_data = await format_chunk_data(weight_metadata, partition.chunk_number)
        partition.optimizer_state_data = await format_chunk_data(optimizer_state_metadata, partition.chunk_number)
        return partition

    async def download_and_set_weights_and_optimizer_state(
        self, layer_idx: int, device: str, parser: Callable = None, epoch: int = None
    ) -> tuple[torch.Tensor, torch.Tensor] | None:
        """Downloads the weights for a given layer and device.

        Miners/validators have to download the shards of the final merged layer weights. These weights exist across multiple files and are 1D byte strings

        Args:
            layer_idx (int): The layer index to download weights for
            device (str): The device to download weights for
            parser (Callable, optional): A parser function to parse the response from the API. Defaults to None.
            epoch (int, optional): The epoch to download weights for. Defaults to None.

        Returns:
            tuple[torch.Tensor, torch.Tensor] | None: The weights and optimizer state for the layer
        """

        if epoch == 1:
            logger.info("Run level epoch == 1, Initializing with random weights to initialize training. 🎲")
            return

        partition_download_error_counter: int = 0

        try:
            response: list[MinerPartition] = await CommonAPIClient.get_merged_partitions(hotkey=self.wallet.hotkey)

            merged_partitions: list[MinerPartition] = await parser(response) if parser else response
            total_parts = len(merged_partitions) if merged_partitions else 0

            if not merged_partitions:
                logger.warning(f"No merged partitions found for epoch {epoch} for miner {self.hotkey[:8]}❗️")
                return

            logger.info(
                f"Preparing to download {total_parts} merged partitions for layer {layer_idx} and epoch {epoch}"
            )

            # Allocate memory to the full 1d tensor, clone to avoid modifying the original weights in place.
            new_weights = torch.nn.utils.parameters_to_vector(self.model_manager.model.parameters()).clone()
            check_for_nans_and_infs(
                new_weights, f"current weights for miner {self.hotkey[:8]}", exception_type=NanInfWarning
            )

            new_optimizer_state, tensor_shapes, state_dict = flatten_optimizer_state(
                optimizer=self.model_manager.optimizer, device=device
            )
            check_for_nans_and_infs(
                new_optimizer_state,
                f"current optimizer state for miner {self.hotkey[:8]}",
                exception_type=NanInfWarning,
            )

            for idx, partition in enumerate(merged_partitions):
                logger.info(f"Downloading merged partition {idx + 1}/{total_parts} (chunk {partition.chunk_number})")
                new_partition: MinerPartition = await self.build_chunk_data(partition)

                try:
                    weight_shard = await download_activation(
                        path=new_partition.weight_path,
                        device=device,
                    )

                    shard_optimizer_state = await download_activation(
                        path=new_partition.optimizer_state_path,
                        device=device,
                    )

                    check_for_nans_and_infs(
                        weight_shard,
                        f"weight shard downloaded for miner {self.hotkey[:8]}",
                        exception_type=NanInfWarning,
                    )
                    check_for_nans_and_infs(
                        shard_optimizer_state,
                        f"shard optimizer state downloaded for miner {self.hotkey[:8]}",
                        exception_type=NanInfWarning,
                    )
                    logger.debug(
                        f"Weight shard shape: {weight_shard.shape}, expected start idx: {new_partition.weight_data.chunk_start_idx}, expected end idx: {new_partition.weight_data.chunk_end_idx}, range: {new_partition.weight_data.chunk_end_idx-new_partition.weight_data.chunk_start_idx}"
                    )
                    new_weights[
                        new_partition.weight_data.chunk_start_idx : new_partition.weight_data.chunk_end_idx
                    ] = weight_shard

                    logger.debug(
                        f"Shard optimizer state shape: {shard_optimizer_state.shape}, expected start idx: {new_partition.optimizer_state_data.chunk_start_idx}, expected end idx: {new_partition.optimizer_state_data.chunk_end_idx}, range: {new_partition.optimizer_state_data.chunk_end_idx-new_partition.optimizer_state_data.chunk_start_idx}"
                    )
                    new_optimizer_state[
                        new_partition.optimizer_state_data.chunk_start_idx : new_partition.optimizer_state_data.chunk_end_idx
                    ] = shard_optimizer_state
                    logger.debug(
                        f"Applied partition {partition.chunk_number}: weights[{new_partition.weight_data.chunk_start_idx}:{new_partition.weight_data.chunk_end_idx}] optimizer[{new_partition.optimizer_state_data.chunk_start_idx}:{new_partition.optimizer_state_data.chunk_end_idx}]"
                    )

                except Exception as e:
                    logger.warning(f"Error downloading partition {partition}: {e}")
                    partition_download_error_counter += 1

            logger.debug(
                f"Downloaded {total_parts - partition_download_error_counter} / {total_parts} partitions inside download_and_set_weights_and_optimizer_state for hotkey {self.hotkey[:8]}"
            )

            # assign weights to self.model
            # reshape thecomplete 1D tensor into the appropriate shape
            new_optimizer_state_dict: dict = reconstruct_optimizer_state(
                flat_tensor=new_optimizer_state, tensor_shapes=tensor_shapes, state_dict=state_dict
            )

            logger.info(
                f"⏳ Setting model weights and optimizer state for layer {layer_idx} for miner {self.hotkey[:8]} on download"
            )
            await self.model_manager.set_model_weights_and_optimizer_state(
                model_weights=new_weights, optimizer_state=new_optimizer_state_dict
            )

            logger.success(
                f"✅ Successfully downloaded and applied weights and optimizer state to model for layer {layer_idx}"
            )

        except Exception:
            raise
