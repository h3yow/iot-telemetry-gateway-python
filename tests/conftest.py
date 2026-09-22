"""pytest 公共夹具：每个用例独立临时快照的网关。"""
from __future__ import annotations

import pytest

from app.service import Gateway


@pytest.fixture
def gateway(tmp_path):
    return Gateway(store_path=tmp_path / "state.json")


@pytest.fixture
def registered(gateway):
    """注册一台设备并签发凭据，返回 (device_id, token)。"""
    gateway.auth.register_device("dev-1")
    issued = gateway.auth.issue_credential("dev-1")
    return "dev-1", issued["token"]
