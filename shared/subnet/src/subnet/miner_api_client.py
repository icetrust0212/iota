from common.models.api_models import (
    ActivationResponse,
    CompleteFileUploadResponse,
    FileUploadCompletionRequest,
    FileUploadRequest,
    FileUploadResponse,
    GetTargetsRequest,
    LossReportRequest,
    MinerRegistrationResponse,
    SubmitActivationRequest,
    SubmittedWeightsAndOptimizerPresigned,
    SyncActivationAssignmentsRequest,
    WeightUpdate,
)
from common.models.error_models import BaseErrorModel
from common.utils.partitions import MinerPartition
from common.utils.s3_utils import upload_parts
from common.utils.shared_states import LayerPhase
from loguru import logger
from subnet.common_api_client import CommonAPIClient
from substrateinterface.keypair import Keypair
from common import settings as common_settings
import requests
import time

class MinerAPIClient(CommonAPIClient):
    @classmethod
    async def get_targets(cls, get_targets_request: GetTargetsRequest, hotkey: Keypair) -> str | BaseErrorModel:
        try:
            response = await cls.orchestrator_request(
                method="POST", path="/miner/get_targets", hotkey=hotkey, body=get_targets_request.model_dump()
            )
            if "error_name" in response:
                return response
            return response
        except Exception as e:
            logger.error(f"Error getting targets: {e}")
            raise

    @classmethod
    async def estimate_layer(cls, hotkey: Keypair):
        """
        Returns:
           int: The layer number which is estimated by orchestrator

        This method:
        1. Determines the layer with the least miners
        2. Assigns the miner to that layer
        3. Updates the miner's layer in the registry
        """
        logger.warning(f"Estimating layer..{hotkey.ss58_address}")
        # Get count of miners per layer
        layer_counts = {l: 0 for l in range(common_settings.N_LAYERS)}
        URL = "https://iota.macrocosmos.ai/api/mainnet/miners"
        current_layer = None

        try:
            resp = requests.get(URL, timeout=15)
            miners_grid_status = resp.json()
            for miner in miners_grid_status["miners"]:
                if miner["layer"] is not None:
                    layer = miner["layer"]
                    layer_counts[layer] += 1
                if miner["hotkey"] == hotkey.ss58_address:
                    current_layer = miner["layer"]

            logger.info(f"Layer counts: {layer_counts}")

            chosen_layer = 0

            # Get the layer with the least miners
            if any(layer_counts[l] != layer_counts[0] for l in range(common_settings.N_LAYERS)):
                min_layer = min(layer_counts, key=layer_counts.get)
                chosen_layer = min_layer

            # If the number of miners in each layer is the same, add to the layer with the least mean incentive
            else:
                chosen_layer = 0

            logger.info(f"Chosen layer: {chosen_layer}")
            return chosen_layer, current_layer
        except Exception as err:
            logger.warning(f"Error while request layer: {err}")
            return None, None


    @classmethod
    async def register_miner_request(cls, hotkey: Keypair) -> MinerRegistrationResponse | dict:
        try:
            current_layer = None
            DESIRED_LAYERS = [common_settings.N_LAYERS - 1]
            current_time = time.time()
            TIME_LIMIT = 300  # seconds

            while True:
                estimated_layer, current_layer = await cls.estimate_layer(hotkey=hotkey)
                logger.info(f"Estimated layer: {estimated_layer}, current layer: {current_layer}")
                if current_layer is not None:
                    break
                if estimated_layer in DESIRED_LAYERS:
                    break
                if time.time() - current_time > TIME_LIMIT:
                    break
            response = await cls.orchestrator_request(method="POST", path="/miner/register", hotkey=hotkey)
            if "error_name" in response:
                return response
            return MinerRegistrationResponse(**response)
        except Exception as e:
            logger.error(f"Error registering miner: {e}")
            raise

    @classmethod
    async def get_layer_state_request(cls, hotkey: Keypair) -> LayerPhase | dict:
        try:
            response = await cls.orchestrator_request(method="GET", path="/miner/layer_state", hotkey=hotkey)
            if "error_name" in response:
                return response
            return LayerPhase(response)
        except Exception as e:
            logger.error(f"Error getting layer state: {e}")
            raise

    @classmethod
    async def get_activation(cls, hotkey: Keypair) -> ActivationResponse | dict:
        try:
            response = await cls.orchestrator_request(method="GET", path="/miner/get_activation", hotkey=hotkey)
            if "error_name" in response:
                return response
            return ActivationResponse(**response)
        except Exception as e:
            logger.error(f"Error getting activation: {e}")
            raise

    @classmethod
    async def initiate_file_upload_request(
        cls,
        hotkey: Keypair,
        file_upload_request: FileUploadRequest,
    ) -> FileUploadResponse | dict:
        try:
            response = await cls.orchestrator_request(
                method="POST",
                path="/miner/initiate_file_upload",
                hotkey=hotkey,
                body=file_upload_request.model_dump(),
            )
            if "error_name" in response:
                return response
            return FileUploadResponse(**response)
        except Exception as e:
            logger.error(f"Error initiating file upload: {e}")
            raise

    @classmethod
    async def upload_multipart_to_s3(cls, urls: list[str], data: bytes, upload_id: str) -> list[dict]:
        parts = await upload_parts(urls=urls, data=data, upload_id=upload_id)
        return parts

    @classmethod
    async def complete_file_upload_request(
        cls,
        hotkey: Keypair,
        file_upload_completion_request: FileUploadCompletionRequest,
    ) -> CompleteFileUploadResponse | dict:
        try:
            response = await cls.orchestrator_request(
                method="POST",
                path="/miner/complete_multipart_upload",
                hotkey=hotkey,
                body=file_upload_completion_request.model_dump(),
            )
            if "error_name" in response:
                return response
            return CompleteFileUploadResponse(**response)
        except Exception as e:
            logger.error(f"Error completing file upload: {e}")
            raise

    @classmethod
    async def submit_weights(
        cls,
        hotkey: Keypair,
        weight_update: WeightUpdate,
    ) -> dict:
        """Attempts to submit weights to the orchestrator"""
        try:
            response = await cls.orchestrator_request(
                method="POST",
                path="/miner/submit_weights",
                hotkey=hotkey,
                body=weight_update.model_dump(),
            )
            return response
        except Exception as e:
            logger.error(f"Error submitting weights: {e}")
            raise

    @classmethod
    async def report_loss(cls, hotkey: Keypair, loss_report: LossReportRequest):
        """Report loss to orchestrator"""
        try:
            response = await cls.orchestrator_request(
                method="POST",
                path="/miner/report_loss",
                hotkey=hotkey,
                body=loss_report.model_dump(),
            )
            return response
        except Exception as e:
            logger.exception(f"Error reporting loss: {e}")
            raise e

    @classmethod
    async def submit_activation_request(
        cls, hotkey: Keypair, submit_activation_request: SubmitActivationRequest
    ) -> ActivationResponse | dict:
        try:
            response = await cls.orchestrator_request(
                method="POST",
                path="/miner/submit_activation",
                hotkey=hotkey,
                body=submit_activation_request.model_dump(),
            )
            return response
        except Exception as e:
            logger.error(f"Error submitting activation: {e}")
            raise

    @classmethod
    async def sync_activation_assignments(cls, activation_ids: list[str], hotkey: Keypair) -> dict[str, bool]:
        try:
            response = await cls.orchestrator_request(
                method="POST",
                path="/miner/sync_activation_assignments",
                hotkey=hotkey,
                body=SyncActivationAssignmentsRequest(activation_ids=activation_ids).model_dump(),
            )
            return response
        except Exception as e:
            logger.error(f"Error checking if activation is active: {e}")
            raise

    @classmethod
    async def get_partitions(cls, hotkey: Keypair) -> list[int] | dict:
        """Get the partition indices for a given hotkey."""
        try:
            response: list[MinerPartition] | dict = await cls.orchestrator_request(
                method="GET",
                path="/miner/get_partitions",
                hotkey=hotkey,
            )
            if "error_name" in response:
                return response

            return response

        except Exception as e:
            logger.error(f"Error getting weight partition info: {e}")
            raise

    @classmethod
    async def get_weight_path_per_layer(cls, hotkey: Keypair) -> list[SubmittedWeightsAndOptimizerPresigned] | dict:
        """Get the weight path for a given layer."""
        try:
            response: list[SubmittedWeightsAndOptimizerPresigned] | dict = await cls.orchestrator_request(
                method="GET",
                path="/miner/get_weight_path_per_layer",
                hotkey=hotkey,
            )
            if "error_name" in response:
                return response
            response = [SubmittedWeightsAndOptimizerPresigned(**weight) for weight in response]
            return response

        except Exception as e:
            logger.error(f"Error getting weight path per layer: {e}")
            raise

    @classmethod
    async def get_num_splits(cls, hotkey: Keypair) -> int | dict:
        """Get the number of splits for a given hotkey."""
        try:
            response: int | dict = await cls.orchestrator_request(
                method="GET",
                path="/miner/get_num_splits",
                hotkey=hotkey,
            )
            return response
        except Exception as e:
            logger.error(f"Error getting number of splits: {e}")
            raise

    @classmethod
    async def get_learning_rate(cls, hotkey: Keypair) -> float | dict:
        """Get the current learning rate."""
        try:
            response: float = await cls.orchestrator_request(method="GET", path="/miner/learning_rate", hotkey=hotkey)
            return response
        except Exception as e:
            logger.error(f"Error getting learning rate: {e}")
            raise

    @classmethod
    async def submit_merged_partitions(cls, hotkey: Keypair, merged_partitions: list[MinerPartition]) -> dict:
        """Submit merged partitions to the orchestrator."""
        try:
            response: dict = await cls.orchestrator_request(
                method="POST",
                path="/miner/submit_merged_partitions",
                hotkey=hotkey,
                body=[partition.model_dump() for partition in merged_partitions],
            )
            return response
        except Exception as e:
            logger.error(f"Error submitting merged partitions: {e}")
            raise
