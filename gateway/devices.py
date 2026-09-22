"""设备注册与查询（内存表，持久化由 service/store 负责）。"""

from __future__ import annotations

from .errors import ErrorCode, GatewayError
from .models import Device


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}

    def register(self, device_id: str) -> Device:
        if not device_id or not isinstance(device_id, str):
            raise GatewayError(ErrorCode.TELEMETRY_BAD_FORMAT, "device_id 非法")
        if device_id in self._devices:
            raise GatewayError(
                ErrorCode.DEVICE_EXISTS,
                f"设备 {device_id} 已注册",
                device_id=device_id,
            )
        device = Device(device_id=device_id)
        self._devices[device_id] = device
        return device

    def get(self, device_id: str) -> Device:
        device = self._devices.get(device_id)
        if device is None:
            raise GatewayError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"设备 {device_id} 未注册",
                device_id=device_id,
            )
        return device

    def has(self, device_id: str) -> bool:
        return device_id in self._devices

    def all(self) -> list[Device]:
        return list(self._devices.values())
