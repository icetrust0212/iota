from typing import Union
from common.utils.exceptions import NanInfWarning
from loguru import logger

import torch


def add_artificial_gradients(model: torch.nn.Module, device: Union[str, torch.device]):
    """Add artificial gradients to the model parameters."""
    model.to(device)
    for param in model.parameters():
        if param.requires_grad:
            param.grad = torch.zeros_like(param.data).to(dtype=torch.bfloat16).to(device)


def flatten_optimizer_state(
    optimizer: torch.optim.Optimizer, device: Union[str, torch.device], dtype=torch.bfloat16
) -> tuple[torch.Tensor, list[tuple[int, ...]], dict]:
    """Flatten all tensors in optimizer state dict into a single tensor."""
    state_dict = optimizer.state_dict()
    tensors = []
    tensor_shapes = []

    for group in state_dict["state"].values():
        for k, v in group.items():
            if k == "step":
                continue
            if isinstance(v, torch.Tensor):
                tensors.append(v.flatten().to(dtype).to(device))
                tensor_shapes.append(v.shape)

    flat_tensor = torch.cat(tensors)
    return flat_tensor, tensor_shapes, state_dict


def reconstruct_optimizer_state(
    flat_tensor: torch.Tensor, tensor_shapes: list[tuple[int, ...]], state_dict: dict
) -> dict:
    """Reconstruct optimizer state dict from flattened tensor."""
    new_state_dict = state_dict.copy()

    start_idx = 0
    tensor_idx = 0
    for group in new_state_dict["state"].values():
        for k, v in group.items():
            if k == "step":
                continue
            if isinstance(v, torch.Tensor):
                numel = v.numel()
                tensor_data = flat_tensor[start_idx : start_idx + numel]
                group[k] = tensor_data.reshape(tensor_shapes[tensor_idx])
                start_idx += numel
                tensor_idx += 1

    return new_state_dict


def check_for_nans_and_infs(tensor, name: str | None = None, exception_type: type = NanInfWarning):
    # Check to see if the weights or optimizer state have any nans
    tensor = tensor.to("cpu")
    try:
        num_nans = torch.isnan(tensor).sum()
        if num_nans > 0:
            total = tensor.numel()
            percentage = (num_nans / total) * 100
            logger.error(f"❌ Miner has NaNs in {name} | {num_nans} / {total} = {percentage:.2f}%")
            raise exception_type(f"{name} has NaNs")
        num_infs = torch.isinf(tensor).sum()
        if num_infs > 0:
            total = tensor.numel()
            percentage = (num_infs / total) * 100
            logger.error(f"❌ Miner has Infs in {name} | {num_infs} / {total} = {percentage:.2f}%")
            raise exception_type(f"{name} has Infs")
    except Exception:
        raise
