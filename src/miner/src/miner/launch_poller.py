import asyncio
import json
import os
import sys
import time
import bittensor as bt
import fcntl  # For file locking on Linux
from datetime import datetime, timezone

from subnet.miner_api_client import MinerAPIClient

from miner import settings
from loguru import logger

from common.models.api_models import (
    ActivationResponse,
    MinerRegistrationResponse,
)
from common.utils.exceptions import (
    LayerStateException,
    MinerNotRegisteredException,
    SpecVersionException,
)
from common.models.error_models import LayerStateError, MinerNotRegisteredError, SpecVersionError

logger.remove()
logger.add(sys.stderr, level="DEBUG")
logger.add("polling.log", rotation="10 MB", level="DEBUG", retention=10)

class ActivationPoller:
    def __init__(self, wallet):
        self.layer = None
        self.wallet = wallet

    async def save_activation_to_file(self, activation: ActivationResponse, base_dir: str = "."):
        # Determine file name based on direction and current UTC date
        if not hasattr(activation, "direction") or activation.direction is None:
            return
        direction = activation.direction
        # Save in the same directory as this script
        # base_dir = os.path.dirname(os.path.abspath(__file__))
        filename = f"{direction}_activations.jsonl"
        filepath = os.path.join(base_dir, filename)

        # Prepare data
        data = activation.to_dict() if hasattr(activation, "to_dict") else activation.__dict__
        data["timestamp"] = datetime.utcnow().isoformat()
        line = json.dumps(data)

        # Try to save with file locking and retry on conflict
        while True:
            try:
                with open(filepath, "a") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                    fcntl.flock(f, fcntl.LOCK_UN)
                logger.info(f"Saved activation to {filepath}")
                break
            except Exception as e:
                logger.warning(f"File access conflict, retrying: {e}")
                time.sleep(0.1)  # Wait before retrying

    async def parse_response(self, response: dict):
        if not isinstance(response, dict):
            return response
        if "error_name" not in response:
            return response
        if error_name := response["error_name"]:
            if error_name == LayerStateError.__name__:
                logger.warning(f"Layer state change: {response['error_dict']}")
                error_dict = LayerStateError(**response["error_dict"])
                raise LayerStateException(
                    f"Miner {self.wallet.hotkey[:8]} is moving state from {error_dict.expected_status} to {error_dict.actual_status}"
                )
            if error_name == MinerNotRegisteredError.__name__:
                logger.error(f"Miner not registered error: {response['error_dict']}")
                raise MinerNotRegisteredException(f"Miner {self.wallet.hotkey[:8]} not registered")
            if error_name == SpecVersionError.__name__:
                logger.error(f"Spec version mismatch: {response['error_dict']}")
                raise SpecVersionException(
                    expected_version=response["error_dict"]["expected_version"],
                    actual_version=response["error_dict"]["actual_version"],
                )
        else:
            return response
        
    async def activation_polling(self):
        try:
            response: ActivationResponse | dict = await MinerAPIClient.get_activation(hotkey=self.wallet.hotkey)
            logger.info(f"Response: {response}")
            response = await self.parse_response(response)
            if not response:
                raise Exception("Error getting activation")
            
            await self.save_activation_to_file(response)
        except Exception as e:
            logger.error(f"Error in activation response handler: {e}")

    async def activation_polling_shooter(self, interval: float = 1.0):
        while True:
            if self.layer is not None:
                try:
                    asyncio.create_task(self.activation_polling())
                except Exception as e:
                    logger.error(f"Activation fire-and-forget worker error: {e}")
            await asyncio.sleep(interval)

    def _parse_iso8601(self,ts: str) -> float:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception as e:
            logger.error(f"Failed to parse isoformat time: {e}")
            return time.time()  # fallback if bad format

    async def prune_shooter(self, interval: float = 20):
        while True:
            try:
                await self.prune_old_activations(direction="forward")
                await self.prune_old_activations(direction="failed")
            except Exception as e:
                logger.error(f"Prune error: {e}")
            await asyncio.sleep(interval)

    async def prune_old_activations(self, direction: str, base_dir: str = ".", max_age_minutes = 0.8):
        max_age = max_age_minutes * 60

        # base_dir = os.path.dirname(os.path.abspath(__file__))
        filename = f"{direction}_activations.jsonl"
        filepath = os.path.join(base_dir, filename)

        if not os.path.exists(filepath):
            return

        try:
            now = time.time()
            valid_lines = []
            with open(filepath, "r+") as f:
                # Exclusive lock (blocks other access)
                fcntl.flock(f, fcntl.LOCK_EX)

                # Read and filter
                for line in f:
                    try:
                        data = json.loads(line)
                        timestamp = data.get("timestamp", now)
                        ts = self._parse_iso8601(timestamp) if timestamp else now

                        if now - ts <= max_age:
                            valid_lines.append(line)
                    except json.JSONDecodeError:
                        continue

                # Truncate and rewrite file
                f.seek(0)
                f.truncate()
                f.writelines(valid_lines)

                # Release lock
                fcntl.flock(f, fcntl.LOCK_UN)

            print(f"✅ Pruned to {len(valid_lines)} lines.")
        except Exception as e:
            logger.error(f"Failed to prune outdated activations {e}")

    async def get_registration_data(self, base_dir: str = ".", interval: int = 60):
        """
        Returns the registration data (layer and orchestrator time) in registration_data.jsonl
        """
        # base_dir = os.path.dirname(os.path.abspath(__file__))
        filename = "registration_data.json"
        filepath = os.path.join(base_dir, filename)
        logger.info(f"Registration file path: {filepath}, {os.path.exists(filepath)}")
        while True:
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r") as f:
                        # Optionally lock for reading, but not strictly necessary for counting
                        fcntl.flock(f, fcntl.LOCK_SH)
                        data = json.load(f)
                        self.layer = data["layer"]
                        logger.info(f"Read registration data from registration_data.json: layer: {self.layer}")
                        fcntl.flock(f, fcntl.LOCK_UN)
                except Exception as e:
                    logger.warning(f"Error counting forward activations: {e}")
            else:
                logger.warning(f"Registration file not found")
            await asyncio.sleep(interval)

        

if __name__ == "__main__":
    wallet_name = settings.WALLET_NAME
    wallet_hotkey = settings.WALLET_HOTKEY
    wallet = bt.wallet(name=wallet_name, hotkey=wallet_hotkey)
    poller = ActivationPoller(wallet)

    async def main():
        await asyncio.gather(
            poller.activation_polling_shooter(interval=0.5),
            poller.prune_shooter(),  # adjust interval as needed
            poller.get_registration_data()
        )

    asyncio.run(main())