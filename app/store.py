"""JSON 快照持久化仓库。

- 内存对象 + 每次变更后原子写入 JSON 快照（tmp + os.replace），
  保证「重启后升级状态仍保持一致」；
- 进程内 RLock 串行化变更，供并发升级/遥测使用。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from app.models import (
    Campaign,
    Credential,
    Device,
    DeviceOccupancy,
    FirmwareImage,
    TelemetryStats,
    UpgradeEvent,
)


class Store:
    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._credentials: dict[str, Credential] = {}
        self._stats: dict[str, TelemetryStats] = {}
        self._firmware: dict[str, FirmwareImage] = {}
        self._campaigns: dict[str, Campaign] = {}
        self._occupancy: dict[str, DeviceOccupancy] = {}
        self._upgrade_events: dict[str, list[UpgradeEvent]] = {}
        self._upgrade_event_seq: int = 0
        self.load()

    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 持久化 ----------------------------------------------------------
    def save(self) -> None:
        if self._path is None:
            return
        snapshot = {
            "devices": {k: v.to_dict() for k, v in self._devices.items()},
            "credentials": {k: v.to_dict()
                            for k, v in self._credentials.items()},
            "stats": {k: v.to_dict() for k, v in self._stats.items()},
            "firmware": {k: v.to_dict() for k, v in self._firmware.items()},
            "campaigns": {k: v.to_dict()
                          for k, v in self._campaigns.items()},
            "occupancy": {k: v.to_dict()
                          for k, v in self._occupancy.items()},
            "upgrade_events": {
                k: [e.to_dict() for e in events]
                for k, events in self._upgrade_events.items()},
            "upgrade_event_seq": self._upgrade_event_seq,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._devices = {k: Device.from_dict(v)
                         for k, v in data.get("devices", {}).items()}
        self._credentials = {
            k: Credential.from_dict(v)
            for k, v in data.get("credentials", {}).items()}
        self._stats = {k: TelemetryStats.from_dict(v)
                       for k, v in data.get("stats", {}).items()}
        self._firmware = {k: FirmwareImage.from_dict(v)
                          for k, v in data.get("firmware", {}).items()}
        self._campaigns = {k: Campaign.from_dict(v)
                           for k, v in data.get("campaigns", {}).items()}
        self._occupancy = {
            k: DeviceOccupancy.from_dict(v)
            for k, v in data.get("occupancy", {}).items()}
        self._upgrade_events = {
            k: [UpgradeEvent.from_dict(e) for e in events]
            for k, events in data.get("upgrade_events", {}).items()}
        self._upgrade_event_seq = data.get(
            "upgrade_event_seq",
            max((e.seq for events in self._upgrade_events.values()
                 for e in events), default=0))

    # ---- devices ---------------------------------------------------------
    def put_device(self, device: Device) -> None:
        self._devices[device.device_id] = device

    def get_device(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def list_devices(self) -> list[Device]:
        return list(self._devices.values())

    # ---- credentials -----------------------------------------------------
    def put_credential(self, credential: Credential) -> None:
        self._credentials[credential.credential_id] = credential

    def get_credential(self, credential_id: str) -> Credential | None:
        return self._credentials.get(credential_id)

    def list_credentials(self) -> list[Credential]:
        return list(self._credentials.values())

    # ---- telemetry -------------------------------------------------------
    def get_stats(self, device_id: str) -> TelemetryStats:
        stats = self._stats.get(device_id)
        if stats is None:
            stats = TelemetryStats(device_id=device_id)
            self._stats[device_id] = stats
        return stats

    def put_stats(self, stats: TelemetryStats) -> None:
        self._stats[stats.device_id] = stats

    # ---- firmware --------------------------------------------------------
    def put_firmware(self, fw: FirmwareImage) -> None:
        self._firmware[fw.version] = fw

    def get_firmware(self, version: str) -> FirmwareImage | None:
        return self._firmware.get(version)

    def list_firmware(self) -> list[FirmwareImage]:
        return list(self._firmware.values())

    # ---- campaigns -------------------------------------------------------
    def put_campaign(self, campaign: Campaign) -> None:
        self._campaigns[campaign.campaign_id] = campaign

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        return self._campaigns.get(campaign_id)

    def list_campaigns(self) -> list[Campaign]:
        return list(self._campaigns.values())

    # ---- device occupancy -----------------------------------------------
    def get_occupancy(self, device_id: str) -> DeviceOccupancy | None:
        return self._occupancy.get(device_id)

    def list_occupancy(self) -> list[DeviceOccupancy]:
        return list(self._occupancy.values())

    def put_occupancy(self, occ: DeviceOccupancy) -> None:
        self._occupancy[occ.device_id] = occ

    def remove_occupancy(self, device_id: str) -> None:
        self._occupancy.pop(device_id, None)

    def replace_all_occupancy(self, occs: dict[str, DeviceOccupancy]) -> None:
        self._occupancy = dict(occs)

    # ---- per-device upgrade history -------------------------------------
    def append_upgrade_event(self, event: UpgradeEvent) -> UpgradeEvent:
        """分配全局单调 seq 并追加（调用方需持有 store 锁）。"""
        self._upgrade_event_seq += 1
        event.seq = self._upgrade_event_seq
        self._upgrade_events.setdefault(event.device_id, []).append(event)
        return event

    def list_upgrade_events(self, device_id: str) -> list[UpgradeEvent]:
        """返回该设备按发生顺序（seq 升序）的升级往来副本。"""
        return sorted(self._upgrade_events.get(device_id, []),
                      key=lambda e: e.seq)
