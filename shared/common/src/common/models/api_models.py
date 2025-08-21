from typing import Literal

from common import settings
from fastapi import HTTPException
from pydantic import BaseModel, model_validator


class WeightsUploadResponse(BaseModel):
    urls: list[str]
    upload_id: str


class FileUploadCompletionRequest(BaseModel):
    object_name: str
    upload_id: str
    parts: list[dict]


class CompleteFileUploadResponse(BaseModel):
    object_path: str


class FileUploadResponse(BaseModel):
    object_name: str
    urls: list[str]
    upload_id: str


class FileUploadRequest(BaseModel):
    num_parts: int
    file_type: Literal["weights", "optimizer_state", "activation", "weights_metadata", "optimizer_state_metadata"]

    @model_validator(mode="after")
    def validate_num_parts(self):
        if self.num_parts < 1:
            raise HTTPException(status_code=400, detail="Number of parts must be at least 1")
        if self.num_parts > settings.MAX_NUM_PARTS:
            raise HTTPException(status_code=400, detail="Number of parts must be less than 1000")

        return self


class GetTargetsRequest(BaseModel):
    activation_id: str | None = None


class SubmitActivationRequest(BaseModel):
    direction: Literal["forward", "backward"]
    activation_id: str | None = None
    activation_path: str | None = None


class SyncActivationAssignmentsRequest(BaseModel):
    activation_ids: list[str]


class WeightUpdate(BaseModel):
    weights_path: str
    weights_metadata_path: str
    optimizer_state_path: str
    optimizer_state_metadata_path: str


class MinerRegistrationResponse(BaseModel):
    layer: int | None = None
    current_epoch: int | None = None


class ValidatorRegistrationResponse(BaseModel):
    layer: int | None = None
    miner_uid_to_track: int
    miner_hotkey_to_track: str


class LossReportRequest(BaseModel):
    activation_id: str
    loss: float


class ActivationResponse(BaseModel):
    activation_id: str | None = None
    direction: Literal["forward", "backward"] | None = None
    presigned_upload_url: str | None = None
    upload_id: str | None = None
    presigned_download_url: str | None = None
    reason: str | None = None


class SubmittedWeightsAndOptimizerPresigned(BaseModel):
    layer: int
    weights_path_presigned: str
    weight_metadata_path_presigned: str
    optimizer_state_path_presigned: str
    optimizer_state_metadata_path_presigned: str
    weight_metadata_path: str
    optimizer_state_metadata_path: str
    weighting_factor: int | None = None


#### Validator related models
class ValidationTaskResponse(BaseModel):
    task_type: str
    success: bool
    score: float
    reason: str | None = None


class ValidatorTask(BaseModel):
    function_name: str
    inputs: dict


class TestTaskModel(BaseModel):
    reason: str


class ValidateActivationModel(BaseModel):
    validator_activation_path: str
    miner_activation_path: str
    direction: Literal["forward", "backward"]


class ValidateWeightsAndOptimizerStateModel(BaseModel):
    weights_path: str
    optimizer_state_path: str


class ValidatorResetModel(BaseModel):
    pass


class ValidatorSetBurnRateModel(BaseModel):
    burn_factor: float
