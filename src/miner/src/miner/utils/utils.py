import json
from typing import Literal, Optional

from common.utils.cache import async_lru
from common.utils.formulas import get_num_parts
import torch
from bittensor_wallet import Keypair
from common import settings as common_settings
from common.models.api_models import (
    CompleteFileUploadResponse,
    FileUploadCompletionRequest,
    FileUploadRequest,
    FileUploadResponse,
)
from common.utils.s3_utils import download_file
from loguru import logger
from subnet.miner_api_client import MinerAPIClient


async def create_metadata(weights_tensor: torch.Tensor, num_sections: int) -> dict:
    """Create metadata for a tensor.

    Args:
        weights_tensor (torch.Tensor): The tensor to create metadata for.
        num_sections (int): The number of sections to split the tensor into.

    Returns:
        dict: The metadata for the tensor.
    """
    # Create metadata about the tensor
    tensor_metadata = {
        "dtype": str(weights_tensor.dtype),
        "size": weights_tensor.size(),
        "num_elements": weights_tensor.numel(),
        "element_size": weights_tensor.itemsize,
        "total_bytes": weights_tensor.nbytes,  # this is just weights_tensor.numel() * weights_tensor.itemsize
    }

    # Number of sections to split into (in elements, or indices)
    section_size = weights_tensor.numel() // num_sections
    # Create section metadata
    sections_metadata = {}

    for i in range(int(num_sections)):
        start_idx = i * section_size
        end_idx = start_idx + section_size if i < num_sections - 1 else weights_tensor.numel()

        # Calculate corresponding tensor indices
        start_byte = start_idx * tensor_metadata["element_size"]
        end_byte = end_idx * tensor_metadata["element_size"]

        assert start_byte is not None and end_byte is not None, "Start byte and end byte are missing"
        assert start_idx is not None and end_idx is not None, "Start idx and end idx are missing"

        sections_metadata[i] = {
            "start_byte": start_byte,
            "end_byte": end_byte,
            "start_idx": start_idx,  # e.g for a (100,100) matrix divided into 10 sections, the indices are: 0-999. 1000 - 1999. 2000 - 2999. 3000 - 3999. 4000 - 4999. 5000 - 5999. 6000 - 6999. 7000 - 7999. 8000 - 8999. 9000 - 9999.
            "end_idx": end_idx,
        }

    # Save full tensor metadata
    full_metadata = {"tensor": tensor_metadata, "sections": sections_metadata}

    return full_metadata


@async_lru(maxsize=5000)
async def download_metadata(metadata_path: str) -> dict:
    """Download metadata from a presigned url.

    Args:
        metadata_path (str): The path to the metadata.

    Returns:
        dict: The metadata.
    """
    metadata_bytes: bytes = await download_file(presigned_url=metadata_path)
    if len(metadata_bytes) > 1_000_000:
        logger.warning(f"Metadata is too large: {len(metadata_bytes)} bytes")
        raise ValueError(f"Metadata is too large: {len(metadata_bytes)} bytes")

    metadata: dict = json.loads(metadata_bytes)
    return metadata


async def upload_file(
    hotkey: Keypair,
    data: bytes,
    file_type: Literal["weights", "optimizer_state", "weights_metadata", "optimizer_state_metadata"],
    file_upload_response: Optional[FileUploadResponse] = None,
) -> str | dict:
    """
    Uploads a file to the orchestrator. To upload, we need to:
    1. Initiate a file upload by getting a FileUploadResponse from the orchestrator
    2. Upload the data using the presigned urls
    3. Complete the file upload

    Args:
        hotkey (Keypair): The hotkey of the miner.
        data (bytes): The data to upload
        file_type (Literal["weights", "optimizer_state"]): The type of file to upload
        file_upload_response (Optional[FileUploadResponse], optional): The response from the orchestrator. Defaults to None.

    Raises:
        ValueError: If the number of parts is greater than the maximum number of parts
        e: If there is an error uploading the file

    Returns:
        str: The path to the uploaded file
    """
    # TODO: We may want to set this to a more optimal value, for now we just make each part 10MB
    try:
        num_parts = get_num_parts(data=data)
        if num_parts > common_settings.MAX_NUM_PARTS:
            raise ValueError(
                f"Number of parts must be less than {common_settings.MAX_NUM_PARTS}. Your file with {len(data)} bytes doesn't fit within {common_settings.MAX_NUM_PARTS} part of 10MB each"
            )

        if file_upload_response is None:
            # Get presigned urls from orchestrator
            file_upload_response: FileUploadResponse | dict = await MinerAPIClient.initiate_file_upload_request(
                hotkey=hotkey,
                file_upload_request=FileUploadRequest(file_type=file_type, num_parts=num_parts),
            )

            # Need to return to check the parsing of the response
            if isinstance(file_upload_response, dict):
                return file_upload_response

        # Upload data to presigned urls
        parts: list[dict] = await MinerAPIClient.upload_multipart_to_s3(
            urls=file_upload_response.urls, data=data, upload_id=file_upload_response.upload_id
        )

        # Complete file upload. Necessary to notify orchestrator that all parts have been uploaded.
        complete_file_upload_response: CompleteFileUploadResponse | dict = (
            await MinerAPIClient.complete_file_upload_request(
                hotkey=hotkey,
                file_upload_completion_request=FileUploadCompletionRequest(
                    object_name=file_upload_response.object_name,
                    upload_id=file_upload_response.upload_id,
                    parts=parts,
                ),
            )
        )

        if isinstance(complete_file_upload_response, dict):
            return complete_file_upload_response

        return complete_file_upload_response.object_path

    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        raise


from urllib.parse import urlparse


def extract_filename_from_url(url):
    """
    Extract the filename from a URL, handling both regular paths and query parameters.

    Args:
        url: The URL to extract filename from


    Returns:
        str: The extracted filename
    """
    # Parse the URL
    parsed_url = urlparse(url)

    # Get the path component
    path = parsed_url.path

    # Extract filename from path
    filename = path.split("/")[-1]

    return filename
