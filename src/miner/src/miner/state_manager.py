import time
import torch
from loguru import logger

from bittensor import Wallet
from pydantic import BaseModel

from common import settings as common_settings
from common.models.miner_models import MinerStatus
from common.utils.shared_states import LayerPhase
from subnet.miner_api_client import MinerAPIClient


class CacheEntry(BaseModel):
    input_activations: torch.Tensor
    output_activations: torch.Tensor
    state: dict
    upload_time: float

    class Config:
        arbitrary_types_allowed = True

from pathlib import Path
from typing import Dict
import os

class DiskSnapshotCache:
    def __init__(self, path: str = "./cache_snapshot.pt"):
        self.path = Path(path)

    def load(self) -> Dict[str, CacheEntry]:
        if not self.path.exists():
            return {}
        # map_location="cpu" avoids GPU init issues on load
        return torch.load(self.path, map_location="cpu")

    def save(self, cache: Dict[str, CacheEntry]) -> None:
        tmp = self.path.with_suffix(".tmp")
        # atomic replace to avoid partial writes if process crashes
        torch.save(cache, tmp)
        os.replace(tmp, self.path)

class StateManager:
    def __init__(self, wallet: Wallet) -> None:
        self.wallet = wallet
        self.layer: int = 0
        self.state: LayerPhase = LayerPhase.TRAINING
        self.direction: MinerStatus = MinerStatus.IDLE
        self.backwards_since_reset: int = 0
        self.processed_activations: int = 0
        self.merge_participation_count: int = 0
        self.completed_optim_steps: int = 0
        self.losses_since_reduce: list = []
        self.backwards_since_reduce: int = 0
        self.backwards_since_sync: int = 0
        self.epoch: int = 0
        self.training_epoch_when_registered: int = None

        self._disk = DiskSnapshotCache("./cache_snapshot.pt")
        self.cache: dict[str, CacheEntry] = self._disk.load()  # load on boot


    def set_state(self, state: LayerPhase):
        self.state = state

    def set_layer(self, layer: int):
        self.layer = layer

    def set_direction(self, direction: MinerStatus):
        self.direction = direction

    def add_to_cache(self, activation_id: str, data: CacheEntry):
        self.cache[activation_id] = data
        self._disk.save(self.cache)  # persist immediately (or batch if you prefer)

    def get_from_cache(self, activation_id: str) -> CacheEntry | None:
        return self.cache.get(activation_id)

    def remove_from_cache(self, activation_id: str):
        del self.cache[activation_id]
        self._disk.save(self._cache)

    async def out_of_cache(self) -> bool:
        if ooc := len(self.cache) >= common_settings.MAX_ACTIVATION_CACHE_SIZE:
            logger.info(
                f"Miner {self.wallet.hotkey} cache full with {len(self.cache)} activations: {self.cache.keys()}"
            )

            # Clean up inactive activations
            activations_to_remove: dict[str, bool] = await MinerAPIClient.sync_activation_assignments(
                activation_ids=list(self.cache.keys()), hotkey=self.wallet.hotkey
            )

            # Remove inactive activations
            for activation_id, is_active in activations_to_remove.items():
                if activation_id in self.cache and not is_active:
                    # Clean up tensors before removing from cache
                    cached_data = self.cache[activation_id]
                    del cached_data  # This will help with garbage collection
                    del self.cache[activation_id]
                    logger.info(f"Removed inactive activation {activation_id} from cache")

            # Update out_of_cache status after cleanup
            ooc = len(self.cache) >= common_settings.MAX_ACTIVATION_CACHE_SIZE

            logger.info(
                f"Miner {self.wallet.hotkey} cache status: {len(self.cache)}/{common_settings.MAX_ACTIVATION_CACHE_SIZE} activations cached, out_of_cache: {ooc}. Cache: {self.cache.keys()}"
            )
        return ooc

    def check_if_timeout(self, timeout: int):
        activations_to_remove: list[str] = []

        if len(self.cache) > 0:
            for activation_id, activation_data in list(self.cache.items()):
                upload_time = activation_data.upload_time
                if upload_time < time.time() - timeout:
                    # Explicitly remove tensor references to help the gc
                    activations_to_remove.append(activation_id)

                    logger.warning(f"🗑️ Removed activation {activation_id} from miner cache due to timeout")

        for activation_id in activations_to_remove:
            self.remove_from_cache(activation_id)

    def reset(self):
        # Clear the cache
        self.cache.clear()
        self.cache = {}

        # Reset the states
        self.state = LayerPhase.TRAINING
        self.direction = MinerStatus.IDLE
        self.backwards_since_reset = 0
        self.processed_activations = 0
        self.merge_participation_count = 0
        self.completed_optim_steps = 0
        self.losses_since_reduce = []
        self.backwards_since_reduce = 0
        self.backwards_since_sync = 0
        self.training_epoch_when_registered = None
