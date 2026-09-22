"""本地固件镜像管理（仅元数据，checksum/size 为模拟值，不依赖外部存储）。"""

from __future__ import annotations

from .errors import ErrorCode, GatewayError
from .models import Firmware


class FirmwareCatalog:
    def __init__(self) -> None:
        self._items: dict[str, Firmware] = {}

    def add(self, firmware: Firmware) -> Firmware:
        if firmware.firmware_id in self._items:
            raise GatewayError(
                ErrorCode.FIRMWARE_EXISTS,
                f"固件 {firmware.firmware_id} 已存在",
            )
        self._items[firmware.firmware_id] = firmware
        return firmware

    def get(self, firmware_id: str) -> Firmware:
        fw = self._items.get(firmware_id)
        if fw is None:
            raise GatewayError(
                ErrorCode.FIRMWARE_NOT_FOUND,
                f"固件 {firmware_id} 不存在",
            )
        return fw

    def all(self) -> list[Firmware]:
        return list(self._items.values())
