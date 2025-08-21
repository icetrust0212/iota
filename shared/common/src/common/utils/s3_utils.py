import asyncio
import math

import aiohttp
from loguru import logger


async def upload_parts(urls: list[str], data: bytes, upload_id: str, max_retries: int = 3) -> list[dict]:
    """Upload parts to S3 storage with retry logic.

    Args:
        urls (list[str]): The URLs to upload the parts to.
        data (bytes): The data to upload.
        upload_id (str): The upload ID.
        max_retries (int): Maximum number of retry attempts per part (default: 3).

    Returns:
        list[dict]: The parts that were uploaded.
    """
    async with aiohttp.ClientSession() as session:
        parts = []

        part_size = int(math.ceil(len(data) / len(urls)))
        chunks = [data[i : i + part_size] for i in range(0, len(data), part_size)]

        logger.info(f"uploading {len(chunks)} chunks with part size {part_size}")

        for i, (url, chunk) in enumerate(zip(urls, chunks)):
            part_number = i + 1
            # Retry logic for each part
            for attempt in range(max_retries + 1):  # +1 to include initial attempt
                try:
                    start_time = asyncio.get_event_loop().time()
                    async with session.put(url, data=chunk) as response:
                        upload_time = asyncio.get_event_loop().time() - start_time

                        if not response.ok:
                            # Get detailed error information from S3
                            error_body = await response.text()
                            error_headers = dict(response.headers)

                            logger.error(f"HTTP Status: {response.status} {response.reason}")
                            logger.error(f"Response Headers: {error_headers}")
                            logger.error(f"Response Body: {error_body}")
                            logger.error(f"Request URL: {url}")
                            logger.error(f"Upload ID: {upload_id}")

                            # Try to parse XML error if it's XML
                            try:
                                import xml.etree.ElementTree as ET

                                root = ET.fromstring(error_body)
                                error_code = root.find(".//Code")
                                error_message = root.find(".//Message")
                                request_id = root.find(".//RequestId")

                                if error_code is not None:
                                    logger.error(f"S3 Error Code: {error_code.text}")
                                    # Check if this is a retryable timeout error
                                    if error_code.text == "RequestTimeout":
                                        raise Exception("RequestTimeout")
                                if error_message is not None:
                                    logger.error(f"S3 Error Message: {error_message.text}")
                                if request_id is not None:
                                    logger.error(f"S3 Request ID: {request_id.text}")
                            except Exception as xml_parse_error:
                                logger.debug(f"Could not parse S3 error XML: {xml_parse_error}")

                        response.raise_for_status()

                        # Extract ETag from response headers (remove quotes if present)
                        etag = response.headers.get("ETag", "").strip('"')
                        # Log upload performance
                        upload_speed_mbps = (len(data) / (1024 * 1024)) / max(upload_time, 0.001)
                        logger.debug(
                            f"🏎️ Part {part_number} upload completed in {upload_time:.2f}s ({upload_speed_mbps:.2f} MB/s) 🏎️"
                        )

                        parts.append(
                            {
                                "PartNumber": part_number,
                                "ETag": etag,
                            }
                        )
                        # Success - break out of retry loop
                        break

                except (
                    aiohttp.ClientError,
                    aiohttp.ServerTimeoutError,
                    aiohttp.ClientResponseError,
                    asyncio.TimeoutError,
                    ConnectionError,
                    Exception,  # Catch RequestTimeout and other S3-specific errors
                ) as e:
                    if attempt < max_retries:
                        # Calculate exponential backoff delay (1s, 2s, 4s, ...)
                        delay = 2**attempt
                        logger.warning(
                            f"Upload failed for part {part_number} (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                            f"Retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Upload failed for part {part_number} after {max_retries + 1} attempts: {e}")
                        raise
    return parts


async def download_file(presigned_url: str):
    """Download a file from S3 storage."""
    timeout = aiohttp.ClientTimeout(total=300)  # 5 minute timeout
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(presigned_url) as response:
                response.raise_for_status()
                return await response.read()
        except aiohttp.ClientResponseError as e:
            if e.status >= 500:
                logger.warning(
                    f"Server error (HTTP {e.status}) downloading file from R2: {e}. This is likely a temporary R2 issue."
                )
            else:
                logger.error(f"HTTP error downloading file: {e}")
            raise
        except Exception as e:
            logger.error(f"Error downloading file from presigned URL: {e}")
            raise
