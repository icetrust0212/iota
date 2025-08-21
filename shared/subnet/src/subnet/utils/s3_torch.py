from time import time

import aiohttp
from common.models.miner_models import ChunkMetadata
from common.utils.exceptions import NanInfWarning
import numpy as np
import torch
from loguru import logger
from subnet.utils.vector_utils import check_for_nans_and_infs
from asyncio.exceptions import TimeoutError


async def download_activation(path: str, dtype: torch.dtype = torch.bfloat16, device: str = "cuda") -> torch.Tensor:
    """Download an activation from S3 storage."""
    try:
        # Download from S3
        timeout = aiohttp.ClientTimeout(total=300)  # 5 minute timeout
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(path) as response:
                response.raise_for_status()
                content = await response.read()
                loaded_tensor = np.frombuffer(content, dtype=np.uint8)
                loaded_tensor = torch.tensor(loaded_tensor).view(dtype).to(device)

        assert isinstance(
            loaded_tensor, torch.Tensor
        ), f"Downloaded tensor is not a torch.Tensor: {type(loaded_tensor)}, path: {path}"

        check_for_nans_and_infs(loaded_tensor, f"activation downloaded from {path}", exception_type=NanInfWarning)

        return loaded_tensor
    except aiohttp.ClientResponseError as e:
        if e.status >= 500:
            logger.warning(
                f"Server error (HTTP {e.status}) downloading activation from R2: {e}. This is likely a temporary R2 issue."
            )
        else:
            logger.error(f"HTTP error downloading activation: {e}")
        raise
    except Exception as e:
        logger.error(f"Error downloading activation: {e}")
        raise


async def download_weights_or_optimizer_state(
    metadata_info: ChunkMetadata,
) -> torch.Tensor:
    """Download weights from S3 storage."""
    start_time = time()
    try:
        timeout = aiohttp.ClientTimeout(total=300)  # 5 minute timeout
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # if partition is not specified, download the full tensor
            byte_range = f"bytes={metadata_info.start_byte}-{metadata_info.end_byte - 1}"
            async with session.get(metadata_info.tensor_path, headers={"Range": byte_range}) as response:
                if response.status > 299:
                    response.raise_for_status()
                binary_data = await response.read()

        section_numpy = np.frombuffer(binary_data, dtype=np.uint8)
        section_torch = torch.from_numpy(section_numpy.copy())
        # assumes default dtype if not specified
        section_torch = section_torch.view(getattr(torch, metadata_info.chunk_dtype))
        return section_torch
    except TimeoutError as e:
        logger.error(
            f"Timeout error downloading weights or optimizer state: {e}. Time taken: {time() - start_time} seconds. Started download at {start_time}."
        )
        raise
    except aiohttp.ClientResponseError as e:
        if e.status >= 500:
            logger.warning(
                f"Server error (HTTP {e.status}) downloading weights or optimizer state from R2: {e}. This is likely a temporary R2 issue."
            )
        else:
            logger.error(f"HTTP error downloading weights or optimizer state: {e}")
        raise
    except Exception as e:
        logger.error(f"Error downloading weights or optimizer state: {e}")
        raise
