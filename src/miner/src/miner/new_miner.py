import asyncio
import json
import time
from datetime import datetime
from typing import Literal, Optional

import torch
import aiohttp
from common.models.miner_models import ChunkMetadata
from miner.utils.partition_merging import download_partition, filter_bad_metadata
from aiohttp import web
from bittensor import Wallet
from common import settings as common_settings
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
    WeightUpdate,
)
from common.models.error_models import LayerStateError, MinerNotRegisteredError, SpecVersionError
from common.utils.exceptions import (
    APIException,
    LayerStateException,
    MinerNotRegisteredException,
    NanInfException,
    NanInfWarning,
    SpecVersionException,
    SubmittedWeightsError,
)
from common.utils.partitions import MinerPartition
from common.utils.s3_utils import download_file
from common.utils.shared_states import LayerPhase
from loguru import logger
from subnet.base.base_neuron import BaseNeuron
from subnet.miner_api_client import MinerAPIClient
from subnet.model.utils import compute_loss
from subnet.test_client import TestAPIClient
from subnet.utils.s3_torch import download_activation
from subnet.utils.vector_utils import check_for_nans_and_infs, flatten_optimizer_state

from miner import settings as miner_settings
from miner.state_manager import CacheEntry, StateManager
from miner.utils.utils import (
    create_metadata,
    extract_filename_from_url,
    upload_file,
)
import os
import fcntl
import random

class HealthServerMixin:
    health_app_runner: Optional[web.AppRunner] = None
    health_site: Optional[web.TCPSite] = None

    async def _start_health_server(self):
        """Starts the aiohttp web server for healthchecks."""
        app = web.Application()

        async def health_handler(request):
            return web.json_response(
                {
                    "status": "healthy",
                    "hotkey": getattr(self, "hotkey", "N/A"),
                    "layer": getattr(self, "layer", "N/A"),
                    "uid": getattr(self, "uid", "N/A"),
                    "registered": getattr(self, "reregister_needed", True) is False,
                    "timestamp": time.time(),
                }
            )

        app.router.add_get(miner_settings.MINER_HEALTH_ENDPOINT, health_handler)

        self.health_app_runner = web.AppRunner(app)
        await self.health_app_runner.setup()

        self.health_site = web.TCPSite(
            self.health_app_runner, miner_settings.MINER_HEALTH_HOST, miner_settings.MINER_HEALTH_PORT
        )
        if miner_settings.LAUNCH_HEALTH:
            await self.health_site.start()
            logger.info(
                f"Miner {getattr(self, 'hotkey', 'N/A')} healthcheck API started on "
                f"http://{miner_settings.MINER_HEALTH_HOST}:{miner_settings.MINER_HEALTH_PORT}{miner_settings.MINER_HEALTH_ENDPOINT}"
            )

    async def _stop_health_server(self):
        """Stops the aiohttp web server for healthchecks."""
        if self.health_site:
            await self.health_site.stop()
            logger.info(f"Miner {getattr(self, 'hotkey', 'N/A')} healthcheck API site stopped.")
            self.health_site = None
        if self.health_app_runner:
            await self.health_app_runner.cleanup()
            logger.info(f"Miner {getattr(self, 'hotkey', 'N/A')} healthcheck API runner cleaned up.")
            self.health_app_runner = None


class Miner(BaseNeuron, HealthServerMixin):
    def __init__(self, wallet_name: str | None = None, wallet_hotkey: str | None = None, wallet: Wallet | None = None):
        super().__init__()
        self.registration_time: str = datetime.now().isoformat()
        self.init_neuron(wallet_name=wallet_name, wallet_hotkey=wallet_hotkey, mock=common_settings.MOCK, wallet=wallet)
        self.state_manager: StateManager = StateManager(wallet=self.wallet)
        self.weights_submitted: bool = False
        self.partitions_submitted: bool = False

    async def run(self):
        logger.info(f"🚀 Starting miner {self.hotkey[:8]} | Timeout: {miner_settings.TIMEOUT}s")
        while True:
            with logger.contextualize(hotkey=self.hotkey[:8], layer=self.state_manager.layer):
                try:
                    if not await MinerAPIClient.check_orchestrator_health(hotkey=self.wallet.hotkey):
                        logger.info(
                            f"🔄 Orchestrator health check failed for miner {self.wallet.hotkey.ss58_address[:8]}"
                        )
                        await self.reset_entire_miner_state()

                        # A small delay before continuing might be beneficial.
                        await asyncio.sleep(5)
                        continue

                    # Final memory check after loading
                    if torch.cuda.is_available():
                        allocated_memory = torch.cuda.memory_allocated() / 1024**3  # GB
                        logger.debug(f"💾 GPU memory: {allocated_memory:.2f}GB")

                    if self.state_manager.state == LayerPhase.TRAINING:
                        await self.step()
                        self.weights_submitted = False
                        self.partitions_submitted = False

                    elif self.state_manager.state == LayerPhase.WEIGHTS_UPLOADING:
                        logger.info(
                            f"\n\n\n\n\n\n\n\n 🔄 Miner {self.hotkey[:8]} in layer {self.state_manager.layer} submitting weights state!\n\n\n\n\n\n\n\n"
                        )
                        if self.weights_submitted:
                            logger.debug(f"Weights already submitted for miner {self.hotkey[:8]}, skipping")
                        else:
                            await self.submit_weights()
                            self.weights_submitted = True
                        logger.info(f"🔄 Miner {self.hotkey[:8]} submitted weights, switching to merging partitions")
                        await self.wait_for_state(state=LayerPhase.MERGING_PARTITIONS)

                    elif self.state_manager.state == LayerPhase.MERGING_PARTITIONS:
                        logger.info(
                            f"\n\n\n\n\n\n\n\n 🔄 Miner {self.hotkey[:8]} in layer {self.state_manager.layer} merging partitions state!\n\n\n\n\n\n\n\n"
                        )
                        if not self.partitions_submitted:
                            logger.info(f"🔄 Miner {self.hotkey[:8]} getting weight partition info")
                            weight_path_per_layer, partitions = await self.get_weight_partition_info()

                            if partitions:
                                logger.info(f"🔄 Miner {self.hotkey[:8]} merging partitions")
                                partitions = await self.merge_partitions(
                                    weight_path_per_layer=weight_path_per_layer,
                                    partitions=partitions,
                                )
                                response = await MinerAPIClient.submit_merged_partitions(
                                    hotkey=self.wallet.hotkey, merged_partitions=partitions
                                )
                                await self.parse_response(response=response)

                            logger.info(f"🔄 Miner {self.hotkey[:8]} merged partitions")
                            self.partitions_submitted = True
                            await self.wait_for_state(state=LayerPhase.TRAINING)
                            await self.reset_entire_miner_state()
                            self.state_manager.epoch += 1

                        else:
                            logger.info(f"🔄 Miner {self.hotkey[:8]} already submitted partitions, skipping...")
                            await self.wait_for_state(state=LayerPhase.TRAINING)

                    logger.info(
                        f"🔄 Miner {self.hotkey[:8]} in Layer {self.state_manager.layer} is in state: {self.state_manager.state}"
                    )
                    await asyncio.sleep(1.1)

                except LayerStateException as e:
                    logger.info(f"🔄 Miner {self.hotkey[:8]} layer state change...: {e}")
                    continue
                except MinerNotRegisteredException as e:
                    logger.info(f"🔄 Miner {self.hotkey[:8]} miner not registered error: {e}")
                    continue
                except APIException as e:
                    logger.info(f"🔄 Miner {self.hotkey[:8]} API exception: {e}")
                    continue
                except aiohttp.ClientResponseError as e:
                    logger.info(f"🔄 Miner {self.hotkey[:8]} Client response error: {e}")
                    continue
                except NanInfWarning as e:
                    logger.info(f"⚠️ Miner {self.hotkey[:8]} NaN/Inf warning: {e}")
                    continue
                except NanInfException as e:
                    logger.error(f"❌ Miner {self.hotkey[:8]} NaN/Inf exception: {e}")
                    raise
                except Exception:
                    raise

    async def step(self):
        logger.info(
            f"🔄 Miner {self.hotkey[:8]} step | Layer: {self.state_manager.layer} | is_training: {self.state_manager.state}"
        )
        logger.info(
            f"🔄 Miner {self.hotkey[:8]} step | Layer: {self.state_manager.layer} | backwards_since_reset: {self.state_manager.backwards_since_reset}"
        )
        logger.info(
            f"🔄 Miner {self.hotkey[:8]} step | Layer: {self.state_manager.layer} | len(cache): {len(self.state_manager.cache)}"
        )

        # Check if any of the activations in the cache have timed out and remove them
        self.state_manager.check_if_timeout(timeout=common_settings.ACTIVATION_CACHE_TIMEOUT)

        # response: ActivationResponse | dict = await MinerAPIClient.get_activation(hotkey=self.wallet.hotkey)
        # response = await self.parse_response(response)
        # if not response:
        #     raise Exception("Error getting activation")

        # check if activation_queue includes backward activations
        response: ActivationResponse | None = self.pop_activation_from_file("backward")

        logger.info(f"Backward response: {response}")
        if not response:
            if await self.state_manager.out_of_cache():
                return
            
            response = self.pop_activation_from_file("forward")
            logger.info(f"Forward response: {response}")
            if not response:
                response = self.pop_activation_from_file("failed")
                logger.info(f"Failed response: {response}")
                
                if not response:
                    if self.state_manager.layer != 0:
                        # No activation available, just return
                        return
        if not response or response is None:
            logger.info(f"Not available activations..")
            return

        if response.direction == "forward":
            await self.forward(response)
        elif response.direction == "backward":
            await self.backward(response)

    async def download_sample(self, download_url: str) -> torch.Tensor:
        data = await download_file(presigned_url=download_url)
        text = data.decode("utf-8")

        if common_settings.MOCK:
            return torch.randn(size=(100,), dtype=torch.bfloat16).to(miner_settings.DEVICE)

        sample = torch.tensor(self.model_manager.tokenizer.encode(text)).to(miner_settings.DEVICE)
        if len(sample) < common_settings.SEQUENCE_LENGTH:
            raise Exception(f"Sample is too short: {len(sample)} < {common_settings.SEQUENCE_LENGTH}")

        sample = sample[: common_settings.SEQUENCE_LENGTH]
        return sample.unsqueeze(0)

    async def forward(self, activation: ActivationResponse | None = None):
        """
        Performs the forward pass.

        If the layer is 0, it will load the data and upload the initial activation to the API.
        If the layer is not 0, it will download a random forward activation from the API and perform the forward pass.

        The forward pass contains:
        - Downloading the forward activation from the API
        - Performing the forward pass
        - Reporting the loss to the API
        - Performing the backward pass
        """
        if await self.state_manager.out_of_cache():
            logger.warning(
                f"⚠️ Miner {self.hotkey[:8]} is out of cache ({len(self.state_manager.cache)}/{common_settings.MAX_ACTIVATION_CACHE_SIZE}), skipping forward pass until backwards have been performed"
            )
            await asyncio.sleep(1)
            return

        assert (
            activation.presigned_download_url is not None and activation.presigned_upload_url is not None
        ), f"Activation is required for layer {self.state_manager.layer}, activation: {activation}"

        logger.info(
            f"🚀 Starting FORWARD pass for layer {self.state_manager.layer} | Processing activation {activation.activation_id} | Miner: {self.hotkey[:8]}"
        )
        try:
            if self.state_manager.layer == 0:
                # Load text file and tokenize
                input_activations = await self.download_sample(download_url=activation.presigned_download_url)
            else:
                # Download activation from S3
                input_activations = await download_activation(
                    path=activation.presigned_download_url, device=miner_settings.DEVICE
                )
                if not common_settings.MOCK:
                    input_activations = input_activations.reshape(
                        -1,
                        common_settings.SEQUENCE_LENGTH,
                        common_settings.MODEL_CFG.get("bottleneck_dim") or common_settings.MODEL_CFG["emb_dim"],
                    )

            # Perform the actual forward pass
            output_activations, state = await self.model_manager._forward(
                layer=self.state_manager.layer, input_activations=input_activations
            )

            self.state_manager.add_to_cache(
                activation.activation_id,
                CacheEntry(
                    input_activations=input_activations,
                    output_activations=output_activations,
                    state=state,
                    upload_time=time.time(),
                ),
            )

            if self.state_manager.layer == common_settings.N_LAYERS - 1:
                await self.compute_last_layer_loss(
                    output_activations=output_activations,
                    input_activation_response=activation,
                    state=state,
                    input_activations=input_activations,
                )
                return await self.backward(activation=activation)

            # If we are not on the last layer, we just need to upload the activations
            upload_response = await self.upload_tensor(
                tensor=output_activations.detach().clone(),
                direction="forward",
            )
            upload_response = await self.parse_response(response=upload_response)

            response = await MinerAPIClient.submit_activation_request(
                hotkey=self.wallet.hotkey,
                submit_activation_request=SubmitActivationRequest(
                    activation_id=activation.activation_id,
                    activation_path=upload_response.object_path,
                    direction="forward",
                ),
            )
            response = await self.parse_response(response=response)
            logger.info(
                f"✅ Successfully completed FORWARD pass for activation {activation.activation_id} on layer {self.state_manager.layer} | Miner: {self.hotkey[:8]}"
            )
        except Exception as e:
            logger.error(
                f"❌ Error during FORWARD pass for activation {activation.activation_id} on layer {self.state_manager.layer} | Miner: {self.hotkey[:8]}: {e}"
            )
            # Save this activation in separated jsonl file
            await self.save_failed_activation(activation)
            raise e

    async def compute_last_layer_loss(
        self,
        output_activations: torch.Tensor,
        input_activation_response: ActivationResponse,
        state: dict,
        input_activations: torch.Tensor,
    ):
        """
        Performs the backward pass for the last layer.
        """

        initial_activations_path = await MinerAPIClient.get_targets(
            get_targets_request=GetTargetsRequest(activation_id=input_activation_response.activation_id),
            hotkey=self.wallet.hotkey,
        )
        initial_activations_path = await self.parse_response(response=initial_activations_path)

        # Target sample is the initial activations
        sample = await self.download_sample(download_url=initial_activations_path)

        loss: torch.Tensor = compute_loss(
            mock=common_settings.MOCK,
            logits=output_activations,
            targets=sample,
            vocab_size=self.model_manager.vocab_size,
            pad_token_id=self.model_manager.eos_token_id,
            pack=miner_settings.PACK_SAMPLES,
        )

        check_for_nans_and_infs(tensor=loss, name=f"Loss for miner {self.hotkey[:8]}", exception_type=NanInfException)

        logger.info(
            f"📊 Computed loss {loss:.6f} for activation {input_activation_response.activation_id} | Layer: {self.state_manager.layer} | Miner: {self.hotkey[:8]}"
        )

        # Update cache with loss before attempting to report it to handle API errors gracefully
        self.state_manager.add_to_cache(
            input_activation_response.activation_id,
            CacheEntry(
                input_activations=input_activations,
                output_activations=loss,
                state=state,
                upload_time=time.time(),
            ),
        )

        try:
            loss_copy: torch.Tensor = loss.clone().detach()
            response = await MinerAPIClient.report_loss(
                hotkey=self.wallet.hotkey,
                loss_report=LossReportRequest(
                    activation_id=input_activation_response.activation_id, loss=loss_copy.item()
                ),
            )
            response = await self.parse_response(response)
            if hasattr(response, "error_name"):
                return

        except Exception as e:
            logger.error(f"Error reporting loss: {e}")

    async def backward(
        self,
        activation: ActivationResponse,
    ):
        logger.info(
            f"🔄 Starting BACKWARD pass for activation {activation.activation_id} | Layer: {self.state_manager.layer} | Miner: {self.hotkey[:8]}"
        )

        try:
            # Check if activation is in cache
            if activation.activation_id not in self.state_manager.cache:
                logger.warning(f"⚠️ Activation {activation.activation_id} not found in cache, skipping backward pass")
                return
            activation_grads = None
            if self.state_manager.layer != common_settings.N_LAYERS - 1 and common_settings.N_LAYERS > 1:
                # For backward pass, we need to get activations that we have cached forward activations for
                # So we still need to list first, then filter, then randomly select
                activation_grads: torch.Tensor = await download_activation(
                    path=activation.presigned_download_url, device=miner_settings.DEVICE
                )
                if not common_settings.MOCK:
                    activation_grads = activation_grads.reshape(
                        -1,
                        common_settings.SEQUENCE_LENGTH,
                        common_settings.MODEL_CFG.get("bottleneck_dim") or common_settings.MODEL_CFG["emb_dim"],
                    )

            # Get activations from cache and move back to GPU
            cached_activations = self.state_manager.cache[activation.activation_id]

            # Move to GPU and enable gradients only for floating point tensors
            input_activations: torch.Tensor = cached_activations.input_activations.to(miner_settings.DEVICE)
            output_activations: torch.Tensor = cached_activations.output_activations.to(miner_settings.DEVICE)

            state = cached_activations.state

            await self.model_manager._backward(
                layer=self.state_manager.layer,
                output_activations=output_activations,
                activation_grads=activation_grads,
                state=state,
            )

            self.state_manager.backwards_since_reset += 1
            logger.debug(f"Backwards since reset for miner {self.hotkey[:8]}: {self.state_manager.backwards_since_reset}")
            # Handle different cases for input activation gradients
            if common_settings.MOCK:
                input_activation_grads = input_activations.detach().to(torch.bfloat16).cpu()

            elif self.state_manager.layer == 0:
                # Get the embedding layer weight grads instead of the input activations grads
                # This is because input activation grads of the first layer do not exist.
                emb_weight = self.model_manager.model.tok_emb.weight
                grad_size = (
                    common_settings.MODEL_CFG["bottleneck_dim"]
                    if common_settings.MODEL_CFG["bottleneck_dim"] is not None
                    else common_settings.MODEL_CFG["emb_dim"]
                )
                input_activation_grads = emb_weight.grad[: common_settings.SEQUENCE_LENGTH, :grad_size]

                # Detach and convert to bfloat16 to ensure we only save the values
                input_activation_grads = input_activation_grads.detach().to(torch.bfloat16).cpu()

            else:
                input_activation_grads = input_activations.grad

            upload_response: CompleteFileUploadResponse = await self.upload_tensor(
                tensor=input_activation_grads,
                direction="backward",
            )

            response = await MinerAPIClient.submit_activation_request(
                hotkey=self.wallet.hotkey,
                submit_activation_request=SubmitActivationRequest(
                    activation_id=activation.activation_id,
                    activation_path=upload_response.object_path,
                    direction="backward",
                ),
            )
            response = await self.parse_response(response=response)
            # Remove from cache
            self.state_manager.remove_from_cache(activation.activation_id)
            logger.info(
                f"✅ Successfully completed BACKWARD pass for activation {activation.activation_id} | Layer: {self.state_manager.layer} | Miner: {self.hotkey[:8]}"
            )
        except Exception as e:
            logger.error(
                f"❌ Error during BACKWARD pass for activation {activation.activation_id} on layer {self.state_manager.layer} | Miner: {self.hotkey[:8]}: {e}"
            )
            # Save this activation in separated jsonl file
            await self.save_failed_activation(activation)
            raise e
        
    async def upload_tensor(
        self,
        tensor: torch.Tensor,
        direction: Literal["forward", "backward"] = None,
        file_type: Literal["activation", "weights", "optimizer_state"] = "activation",
    ) -> CompleteFileUploadResponse:
        initiate_response: FileUploadResponse | dict = await MinerAPIClient.initiate_file_upload_request(
            hotkey=self.wallet.hotkey,
            file_upload_request=FileUploadRequest(
                file_type=file_type,
                num_parts=1,
            ),
        )
        initiate_response = await self.parse_response(initiate_response)

        if not initiate_response:
            raise Exception("Error initiating file upload")

        check_for_nans_and_infs(
            tensor=tensor,
            name=f"Uploading tensor of file type {file_type} for miner {self.hotkey[:8]}",
            exception_type=NanInfException,
        )

        # Reinterpret tensor memory as bytes in a consistent format (bfloat16 → uint8 bytes)
        # Always upload as bfloat16-backed bytes to match the downloader's default expectation.
        tensor_cpu = tensor.detach().to("cpu").to(torch.bfloat16).contiguous()
        data = tensor_cpu.view(torch.uint8).numpy().tobytes()

        try:
            parts: list[dict] = await MinerAPIClient.upload_multipart_to_s3(
                urls=initiate_response.urls, data=data, upload_id=initiate_response.upload_id
            )
        except Exception as e:
            logger.error(f"Error uploading multipart to S3: {e}")
            raise

        response: CompleteFileUploadResponse | dict = await MinerAPIClient.complete_file_upload_request(
            hotkey=self.wallet.hotkey,
            file_upload_completion_request=FileUploadCompletionRequest(
                object_name=initiate_response.object_name,
                upload_id=initiate_response.upload_id,
                parts=parts,
            ),
        )
        response = await self.parse_response(response=response)

        return response

    async def register_loop(self):
        """
        Register the miner with the orchestrator, acquiring a layer during the process.
        If the miner is not registered, it will try to register every 60 seconds
        """
        while True:
            try:
                if not getattr(self, "registered_on_metagraph", True):
                    logger.warning(
                        f"Miner {self.hotkey[:8]} not registered on metagraph. Sleeping for 1 minute before retry..."
                    )
                    await asyncio.sleep(60)  # 60 seconds

                    # Try to re-register using init_neuron method
                    logger.info(f"🔄 Attempting to re-register miner {self.hotkey[:8]} on metagraph...")
                    self.init_neuron(
                        wallet_name=miner_settings.WALLET_NAME,
                        wallet_hotkey=miner_settings.WALLET_HOTKEY,
                        wallet=self.wallet,
                        mock=common_settings.MOCK,
                    )
                    if not getattr(self, "registered_on_metagraph", True):
                        continue
                    else:
                        logger.success(f"✅ Miner {self.hotkey[:8]} registered successfully on metagraph")

                if not common_settings.BITTENSOR:
                    await TestAPIClient.register_to_metagraph(hotkey=self.wallet.hotkey)

                logger.info(f"🔄 Attempting to register miner {self.hotkey[:8]} with orchestrator...")
                response: MinerRegistrationResponse = await MinerAPIClient.register_miner_request(
                    hotkey=self.wallet.hotkey
                )

                assigned_layer = int(response.layer)
                current_epoch = int(response.current_epoch)

                if response.layer is None:
                    raise Exception(
                        f"Miner {self.hotkey[:8]} registered with no layer assigned, this should not happen"
                    )

                self.state_manager.set_layer(assigned_layer)
                self.state_manager.training_epoch_when_registered = current_epoch

                try:
                    await self.save_registration_data_in_file()
                except Exception as e:
                    logger.error(f"Failed to save registration data into file")
                logger.success(
                    f"✅ Miner {self.hotkey[:8]} registered successfully in layer {self.state_manager.layer} on training epoch {current_epoch}"
                )
                return

            except Exception as e:
                logger.exception(f"Error registering miner: {e}")
                await asyncio.sleep(10)

    async def submit_weights(self):
        """
        Uploads the weights to the orchestrator and submits them to the database

        Raises:
            SubmittedWeightsError: If the weights are not submitted successfully
            e: If there is an error submitting the weights
        """
        if self.state_manager.backwards_since_reset == 0:
            logger.warning(f"Backwards since reset for miner {self.hotkey[:8]} is 0, skipping")
            return
        if all([p.grad is None for p in self.model_manager.model.parameters()]):
            logger.warning(f"Gradients are None for miner {self.hotkey[:8]}, skipping")
            logger.debug(f"Gradients: {[p.grad for p in self.model_manager.model.parameters()]}")
            return

        learning_rate = await MinerAPIClient.get_learning_rate(hotkey=self.wallet.hotkey)
        learning_rate = await self.parse_response(learning_rate)
        await self.model_manager.local_all_reduce(learning_rate=learning_rate)

        flattened_optimizer_state, _, _ = flatten_optimizer_state(
            optimizer=self.model_manager.optimizer, device=miner_settings.DEVICE
        )
        weights = torch.nn.utils.parameters_to_vector(parameters=self.model_manager.model.parameters())

        try:
            num_splits = await MinerAPIClient.get_num_splits(hotkey=self.wallet.hotkey)
            num_splits = await self.parse_response(num_splits)
            if not num_splits:
                raise Exception("Error getting number of splits")

            weight_update_dict = {}
            for name, tensor in {"weights": weights, "optimizer_state": flattened_optimizer_state}.items():
                check_for_nans_and_infs(
                    tensor=tensor, name=f"{name} for miner {self.hotkey[:8]}", exception_type=NanInfException
                )

                metadata_name = f"{name}_metadata"
                metadata: dict = await create_metadata(weights_tensor=tensor, num_sections=num_splits)

                # Convert tensor to bytes, handling bfloat16 compatibility
                tensor_cpu = tensor.detach().to("cpu").contiguous()
                tensor_cpu = tensor_cpu.view(torch.uint8)
                # Convert bfloat16 to float32 for NumPy compatibility, then to bytes
                tensor_bytes = tensor_cpu.numpy().tobytes()
                logger.debug(
                    f"UPLOADING {name} for miner {self.hotkey[:8]}. Elements: {tensor_cpu.numel()}, Dtype: {tensor_cpu.dtype}, Shape: {tensor_cpu.shape}"
                )

                path: str | dict = await upload_file(data=tensor_bytes, file_type=name, hotkey=self.wallet.hotkey)
                path = await self.parse_response(response=path)

                # Upload metadata as activation type since orchestrator doesn't have a metadata type
                metadata_path = await upload_file(
                    data=json.dumps(metadata).encode(), file_type=metadata_name, hotkey=self.wallet.hotkey
                )
                metadata_path = await self.parse_response(response=metadata_path)

                weight_update_dict[name + "_path"] = path
                weight_update_dict[metadata_name + "_path"] = metadata_path

            response: dict = await MinerAPIClient.submit_weights(
                hotkey=self.wallet.hotkey, weight_update=WeightUpdate(**weight_update_dict)
            )
            response = await self.parse_response(response)

            if not response:
                raise SubmittedWeightsError("Error submitting weights")

        except LayerStateException as e:
            logger.debug(f"Layer state exception submitting weights: {e}")
            raise

        except Exception as e:
            logger.error(f"Generic error submitting weights: {e}")
            raise

    async def run_miner(self):
        """
        Run the miner. Responsible for:
        - Starting the healthcheck server
        - Registering the miner
        - Setting up the local model
        - Running the miner loop

        The method runs in a loop and retries on failures with a fixed delay.
        """

        logger.info(f"🚀 Starting miner {self.hotkey[:8]} 🚀")
        try:
            # Start the healthcheck server
            if miner_settings.LAUNCH_HEALTH:
                await self._start_health_server()
                logger.info(f"🏥 Health server started for miner {self.hotkey[:8]}")
            else:
                logger.warning(
                    "⚠️ Miner healthcheck API not configured in settings (MINER_HEALTH_PORT missing). Skipping."
                )

            # Reset the entire miner state, which also downloads the weights and optimizer state.
            await self.reset_entire_miner_state()
            await self.run()

        except KeyboardInterrupt:
            logger.info(f"Gracefully shutting down miner {self.hotkey[:8]}")

        except SpecVersionException:
            logger.error("Spec version mismatch. Please pull the latest code and restart the miner")
            raise

        except Exception as e:
            logger.exception(f"❌ Critical error in run_miner for {self.hotkey[:8]}: {e}")
            await asyncio.sleep(5)

        finally:
            logger.info(f"Cleaning up miner {self.hotkey[:8]} on shutdown...")
            try:
                if hasattr(self, "model_manager"):
                    self.model_manager._clean_gpu_memory()

                if miner_settings.LAUNCH_HEALTH:
                    try:
                        await self._stop_health_server()
                        logger.info(f"🏥 Health server stopped for miner {self.hotkey[:8]}")
                    except Exception as e:
                        logger.error(f"Failed to stop health server for miner {self.hotkey[:8]}: {e}")

            except Exception as e:
                logger.error(f"Failed to shutdown miner {self.hotkey[:8]}: {e}")

        # Final cleanup when exiting the loop (only reached on KeyboardInterrupt)
        logger.info(f"🛑 Miner {self.hotkey[:8]} shutdown complete")

        # Miners can sometimes not clean themselves up properly. Therefore, lets force kill the process.
        import sys

        sys.exit(0)

    async def reset_entire_miner_state(self):
        """
        Reset the entire miner state, including the API client, health server, and all other state.
        """
        logger.info(f"🔄 Resetting miner {self.hotkey[:8]} entire state")

        self.state_manager.reset()

        # TODO: This wont work if we start moving miners across layers depending on the epoch.
        if self.model_manager.model is not None and self.model_manager.optimizer is not None:
            current_model_weights: torch.Tensor = torch.nn.utils.parameters_to_vector(
                self.model_manager.model.parameters()
            )
            current_model_optimizer_state: dict = self.model_manager.optimizer.state_dict()
        else:
            current_model_weights = None
            current_model_optimizer_state = None

        # Reset the state of the model, gpu memory, etc.. to ensure a clean epoch.
        self.model_manager.reset()

        await self.register_loop()

        if not await self._setup_local_model(
            model_weights=current_model_weights,
            optimizer_state=current_model_optimizer_state,
            layer=self.state_manager.layer,
            device=miner_settings.DEVICE,
        ):
            raise Exception("Error setting up local model")

        logger.success(f"✅ Successfully setup local model for miner {self.hotkey[:8]}")

        try:
            await self.download_and_set_weights_and_optimizer_state(
                layer_idx=self.state_manager.layer,
                device=miner_settings.DEVICE,
                parser=self.parse_response,
                epoch=self.state_manager.training_epoch_when_registered,
            )
        except Exception as e:
            logger.exception(
                f"Error downloading and setting weights and optimizer state in miner {self.hotkey[:8]}: {e}"
            )
            raise

    async def get_weight_partition_info(
        self,
    ) -> tuple[list[SubmittedWeightsAndOptimizerPresigned], list[MinerPartition]]:
        """
        Get the weight partition info from the orchestrator. This calls two different API endpoints:
        - /miner/get_weight_path_per_layer (weight path for the model layer)
        - /miner/get_partition_indices_by_hotkey (partition indices for the miner)

        Returns:
            tuple[list[SubmittedWeightsPresigned], list[int]]: The weight partition info and the partition ids
        """
        weight_path_per_layer: list[
            SubmittedWeightsAndOptimizerPresigned
        ] | dict = await MinerAPIClient.get_weight_path_per_layer(hotkey=self.wallet.hotkey)
        weight_path_per_layer = await self.parse_response(weight_path_per_layer)

        if not weight_path_per_layer:
            raise Exception("Error getting weight path per layer")

        logger.debug(f"Miner {self.hotkey[:8]} | layer {self.state_manager.layer} getting partitions")
        partitions: list[MinerPartition] | dict = await MinerAPIClient.get_partitions(hotkey=self.wallet.hotkey)
        partitions = await self.parse_response(partitions)
        logger.debug(f"Miner {self.hotkey[:8]} | layer {self.state_manager.layer} partitions: {partitions}")

        if not partitions:
            logger.warning(f"No partitions found for miner {self.hotkey[:8]}")
            return weight_path_per_layer, []

        return weight_path_per_layer, [MinerPartition(**p) for p in partitions]

    async def merge_partitions(
        self, weight_path_per_layer: list[SubmittedWeightsAndOptimizerPresigned], partitions: list[MinerPartition]
    ) -> list[MinerPartition]:
        """Merge the models from the other miners.

        Args:
            weight_path_per_layer (list[SubmittedWeightsPresigned]): The paths to the other miners' partitions
            partition_ids (list[int]): The partition indices to merge

        Returns:
            list[Partition]: The merged partitions
        """
        final_partitions: list[MinerPartition] = []

        filtered_metadata = await filter_bad_metadata(
            partitions=partitions, submitted_weights_and_optimizers=weight_path_per_layer
        )

        for partition in partitions:
            logger.debug(
                f"Miner {self.hotkey[:8]} | layer {self.state_manager.layer} | merging partition {partition.chunk_number}"
            )

            weight_average = None
            optimizer_state_average = None
            weight_counter = 0
            optimizer_state_counter = 0

            results: list[tuple[torch.Tensor, torch.Tensor]] = await asyncio.gather(
                *[
                    download_partition(
                        weight_metadata=metadata[partition.chunk_number]["weights"],
                        optimizer_metadata=metadata[partition.chunk_number]["optimizer_state"],
                    )
                    for _, metadata in filtered_metadata.items()
                ]
            )
            for metadata, (weights, optimizer_state) in zip(filtered_metadata.values(), results):
                try:
                    if weights is None or optimizer_state is None:
                        logger.warning(
                            f"No weights or optimizer state downloaded for miner {self.hotkey[:8]}. Partitions: {partitions}"
                        )
                        raise Exception(
                            f"No weights or optimizer state downloaded for miner {self.hotkey[:8]}. Partitions: {partitions}"
                        )

                    # TODO: We will be changing the way that weights and optimizer states are merged.
                    weights_metadata: ChunkMetadata = metadata[partition.chunk_number]["weights"]
                    optimizer_state_metadata: ChunkMetadata = metadata[partition.chunk_number]["optimizer_state"]

                    if weight_average is None:
                        weight_average = weights.to(torch.float32) * weights_metadata.weighting_factor
                        optimizer_state_average = (
                            optimizer_state.to(torch.float32) * optimizer_state_metadata.weighting_factor
                        )

                    else:
                        # create a running sum of weights weighted by the weighting factor
                        weight_average += weights.to(torch.float32) * weights_metadata.weighting_factor
                        optimizer_state_average += (
                            optimizer_state.to(torch.float32) * optimizer_state_metadata.weighting_factor
                        )

                    weight_counter += weights_metadata.weighting_factor
                    optimizer_state_counter += optimizer_state_metadata.weighting_factor

                except Exception as e:
                    logger.exception(
                        f"Error downloading chunk {partition.chunk_number} from {metadata} for miner {self.hotkey[:8]}: {e}"
                    )

            if weight_average is None:
                raise Exception(f"No weights downloaded for miner {self.hotkey[:8]}. Partitions: {partitions}")

            # Average the weights
            weight_average /= weight_counter
            weight_average = weight_average.to(torch.bfloat16)
            optimizer_state_average /= optimizer_state_counter
            optimizer_state_average = optimizer_state_average.to(torch.bfloat16)

            weight_upload_response: CompleteFileUploadResponse = await self.upload_tensor(
                tensor=weight_average.detach().cpu(),
                file_type="weights",
            )
            weight_upload_response = await self.parse_response(response=weight_upload_response)

            optimizer_state_upload_response: CompleteFileUploadResponse = await self.upload_tensor(
                tensor=optimizer_state_average.detach().cpu(),
                file_type="optimizer_state",
            )
            optimizer_state_upload_response = await self.parse_response(response=optimizer_state_upload_response)

            partition.weight_path = weight_upload_response.object_path
            partition.optimizer_state_path = optimizer_state_upload_response.object_path
            partition.weight_metadata_path = extract_filename_from_url(weights_metadata.metadata_path)
            partition.optimizer_state_metadata_path = extract_filename_from_url(optimizer_state_metadata.metadata_path)

            final_partitions.append(partition)
        return final_partitions

    async def register(self):
        """
        Register the miner with the orchestrator.
        """
        await self.register_loop()

    async def parse_response(self, response: dict):
        if not isinstance(response, dict):
            return response
        if "error_name" not in response:
            return response
        if error_name := response["error_name"]:
            if error_name == LayerStateError.__name__:
                logger.warning(f"Layer state change: {response['error_dict']}")
                error_dict = LayerStateError(**response["error_dict"])
                self.state_manager.set_state(error_dict.actual_status)
                raise LayerStateException(
                    f"Miner {self.hotkey[:8]} is moving state from {error_dict.expected_status} to {error_dict.actual_status}"
                )
            if error_name == MinerNotRegisteredError.__name__:
                logger.error(f"Miner not registered error: {response['error_dict']}")
                await self.register()
                self.state_manager.reset()
                raise MinerNotRegisteredException(f"Miner {self.hotkey[:8]} not registered")
            if error_name == SpecVersionError.__name__:
                logger.error(f"Spec version mismatch: {response['error_dict']}")
                raise SpecVersionException(
                    expected_version=response["error_dict"]["expected_version"],
                    actual_version=response["error_dict"]["actual_version"],
                )
        else:
            return response

    async def wait_for_state(self, state: LayerPhase):
        while True:
            await asyncio.sleep(5)
            logger.info(f"Waiting for state {state} for miner {self.hotkey[:8]}")
            response = await MinerAPIClient.get_layer_state_request(hotkey=self.wallet.hotkey)
            response = await self.parse_response(response)
            if response == state.value:
                logger.info(f"Orchestrator is finally in state {state}")
                self.state_manager.set_state(LayerPhase.from_str(response))
                break
            elif LayerPhase.from_str(response).next() == state:
                continue
            else:
                self.state_manager.set_state(LayerPhase.TRAINING)
                raise LayerStateException(
                    f"Miner {self.hotkey[:8]} is out of sync with the orchestrator. Miner is waiting for orchestrator to be in state {state}, but orchestrator is in state {response}, setting state to training"
                )
            
    async def save_failed_activation(self, activation: ActivationResponse, base_dir: str = "."):
        # Determine file name based on direction and current UTC date
        if not hasattr(activation, "direction") or activation.direction is None:
            return
        # Save in the same directory as this script
        filename = f"failed_activations.jsonl"
        filepath = os.path.join(base_dir, filename)

        # Prepare data
        data = activation.to_dict() if hasattr(activation, "to_dict") else activation.__dict__
        line = json.dumps(data)

        # Try to save with file locking and retry on conflict
        while True:
            try:
                with open(filepath, "r+") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    lines = f.readlines()

                    # Remove only the activation with matching activation_id
                    activation_id = activation.activation_id
                    kept = []
                    for line in lines:
                        obj = json.loads(line)
                        if obj.get("activation_id") != activation_id:
                            kept.append(line)

                    f.writelines(kept)
                    f.flush()
                    os.fsync(f.fileno())

                    fcntl.flock(f, fcntl.LOCK_UN)
                    logger.info(f"Saved activation to {filepath}")
                break
            except Exception as e:
                logger.warning(f"File access conflict, retrying: {e}")
                time.sleep(0.1)  # Wait before retrying

    def pop_activation_from_file(self, direction: str, base_dir: str = ".", retry_delay: float = 0.1) -> ActivationResponse | None:
        """
        Pops the last activation from the given direction's activations file.
        Uses file locking to avoid race conditions.
        Returns the activation dict or None if file is empty.
        Retries until it can safely access the file.
        """
        filename = f"{direction}_activations.jsonl"
        filepath = os.path.join(base_dir, filename)

        while True:
            try:
                if not os.path.exists(filepath):
                    return None

                with open(filepath, "r+") as f:
                    # Try to acquire an exclusive lock
                    fcntl.flock(f, fcntl.LOCK_EX)
                    lines = f.readlines()
                    if not lines:
                        fcntl.flock(f, fcntl.LOCK_UN)
                        return None

                    # For forward and backward activations, it takes last (latest) activation. For failed activation, it just takes random activations.
                    idx = -1
                    if direction == "forward" or direction == "backward":
                        idx = -1
                    else:
                        idx = random.randrange(len(lines))

                    activation_line = lines[idx]
                    activation = json.loads(activation_line)
                    activation = ActivationResponse(**activation) if isinstance(activation, dict) else None

                    kept = lines[:idx] + lines[idx+1:]

                    # Move file pointer to start and write back the rest
                    f.seek(0)
                    f.truncate()
                    
                    if direction == "forward" or direction == "backward":
                        f.writelines(lines[:-1])
                    else:
                        f.writelines(kept)

                    f.flush()
                    os.fsync(f.fileno())
                    fcntl.flock(f, fcntl.LOCK_UN)
                    return activation

            except Exception as e:
                # If file is locked or another error occurs, wait and retry
                logger.error(f"Error while reading activation files: {e}")
                time.sleep(retry_delay)

    def get_forward_activation_count(self, base_dir: str = ".") -> int:
        """
        Returns the number of forward activations in the forward_activations.jsonl file.
        """
        filename = "forward_activations.jsonl"
        filepath = os.path.join(base_dir, filename)
        count = 0

        if not os.path.exists(filepath):
            return 0

        try:
            with open(filepath, "r") as f:
                # Optionally lock for reading, but not strictly necessary for counting
                fcntl.flock(f, fcntl.LOCK_SH)
                count = sum(1 for _ in f)
                fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"Error counting forward activations: {e}")
            return 0

        return count

    async def save_registration_data_in_file(self, base_dir: str = "."):
        filename = f"registration_data.json"
        filepath = os.path.join(base_dir, filename)

        # Prepare data
        data = {
            "layer": self.state_manager.layer,
        }
        line = json.dumps(data)

        # Try to save with file locking and retry on conflict
        while True:
            try:
                with open(filepath, "w") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                    fcntl.flock(f, fcntl.LOCK_UN)
                logger.info(f"Saved registration data to {filepath}")
                break
            except Exception as e:
                logger.warning(f"File access conflict, retrying: {e}")
                time.sleep(0.1)  # Wait before retrying


if __name__ == "__main__":
    new_miner = Miner(wallet_name=miner_settings.WALLET_NAME, wallet_hotkey=miner_settings.WALLET_HOTKEY)
    asyncio.run(new_miner.run_miner())
