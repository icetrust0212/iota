import copy
import pytest
import torch
import torch.nn as nn
import torch.optim as optim

from subnet.utils.vector_utils import flatten_optimizer_state, reconstruct_optimizer_state


class SimpleModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = nn.Sequential(nn.Linear(10, 64), nn.ReLU(), nn.Linear(64, 32), nn.ReLU(), nn.Linear(32, 1))

    def forward(self, x):
        return self.layers(x)


@pytest.fixture
def optimizer_setup():
    model = SimpleModel()
    optimizer = optim.AdamW(model.parameters(), lr=0.001)

    # Generate training data and do some training steps
    X = torch.randn(100, 10)
    y = torch.randn(100, 1)
    criterion = nn.MSELoss()

    for _ in range(5):
        outputs = model(X)
        loss = criterion(outputs, y)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    return model, optimizer


def test_flatten_optimizer_state(optimizer_setup):
    _, optimizer = optimizer_setup
    flat_tensor, tensor_shapes, state_dict = flatten_optimizer_state(optimizer, device="cpu")

    # Test that we got a flat tensor
    assert isinstance(flat_tensor, torch.Tensor)
    assert len(flat_tensor.shape) == 1

    # Test that shapes were recorded
    assert len(tensor_shapes) > 0
    for shape in tensor_shapes:
        assert isinstance(shape, torch.Size)

    # Test that state dict was preserved
    assert isinstance(state_dict, dict)
    assert "state" in state_dict
    assert "param_groups" in state_dict


def test_reconstruct_optimizer_state(optimizer_setup):
    model1, optimizer = optimizer_setup
    model2 = copy.deepcopy(model1)
    optimizer2 = optim.Adam(model2.parameters(), lr=0.001)

    # Flatten and reconstruct
    flat_tensor, tensor_shapes, state_dict = flatten_optimizer_state(optimizer, device="cpu")
    reconstruct_optimizer_state(flat_tensor, tensor_shapes, state_dict, optimizer2)

    # Test that optimizers have same state dict
    assert str(optimizer.state_dict()) == str(
        optimizer2.state_dict()
    ), "Reconstructed optimizer state does not match original"

    # Test that optimizers behave the same
    X = torch.randn(10, 10)
    y = torch.randn(10, 1)
    criterion = nn.MSELoss()

    # Get gradients from original optimizer
    outputs1 = model1(X)
    loss1 = criterion(outputs1, y)
    optimizer.zero_grad()
    loss1.backward()
    optimizer.step()
    optimizer.zero_grad()
    loss12 = criterion(outputs1, y)
    loss12.backward()

    # Get gradients from reconstructed optimizer
    outputs2 = model2(X)
    loss2 = criterion(outputs2, y)
    optimizer2.zero_grad()
    loss2.backward()
    optimizer2.step()
    optimizer2.zero_grad()
    loss22 = criterion(outputs2, y)
    loss22.backward()

    # Test losses are equal
    assert abs(loss1.item() - loss2.item()) < 1e-5
    assert abs(loss12.item() - loss22.item()) < 1e-5
