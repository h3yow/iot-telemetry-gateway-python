"""JSON 快照持久化仓库。

- 内存对象 + 每次变更后原子写入 JSON 快照（tmp + os.replace），
  保证「重启后升级状态仍保持一致」；
- 进程内 RLock 串行化变更，供并发升级/遥测使用；
- 加载快照后从活动批次状态重建设备占用：旧版本快照没有
  device_occupations 字段（原地升级场景），或快照里残留指向
  已终态批次的占用时，都以「进行中批次」为准自愈——只要批次没走到
  成功/回滚/取消，它名下的设备就仍被占着；终态批次的占用必被清掉。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from app.models import (
    BatchState,
    Campaign,
    Credential,
    Device,
    DeviceOccupation,
    DeviceUpgradeState,
    FirmwareImage,
    TelemetryStats,
    UpgradeEvent,
)

logger = logging.getLogger("iot.store")


class Store:
    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._credentials: dict[str, Credential] = {}
        self._stats: dict[str, TelemetryStats] = {}
        self._firmware: dict[str, FirmwareImage] = {}
        self._campaigns: dict[str, Campaign] = {}
        self._occupations: dict[str, DeviceOccupation] = {}
        self._upgrade_events: dict[str, list[UpgradeEvent]] = {}
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
            "device_occupations": {
                k: v.to_dict() for k, v in self._occupations.items()},
            "upgrade_events": {
                k: [e.to_dict() for e in events]
                for k, events in self._upgrade_events.items()},
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
        self._occupations = {
            k: DeviceOccupation.from_dict(v)
            for k, v in data.get("device_occupations", {}).items()}
        self._upgrade_events = {
            k: [UpgradeEvent.from_dict(e) for e in events]
            for k, events in data.get("upgrade_events", {}).items()}
        self._rebuild_occupations()

    def _rebuild_occupations(self) -> None:
        """以「进行中批次」为准重建设备占用（幂等，重启多少次结果一致）。

        - 旧版本快照没有 device_occupations 字段（原地升级沿用旧快照），
          这里从活动批次状态推导回来：进行中批次里仍在 UPGRADING 的设备
          一律视为被该活动/批次占用；
        - 快照里指向已终态/不存在批次的占用一律清掉，不会卡住不释放；
        - 快照里已有的有效占用保留原 acquired_at，不抖动。
        """
        rebuilt: dict[str, DeviceOccupation] = {}
        for campaign in self._campaigns.values():
            for batch in campaign.batches:
                if batch.state != BatchState.IN_PROGRESS:
                    continue
                for device_id, up in batch.upgrades.items():
                    if up.state != DeviceUpgradeState.UPGRADING:
                        continue
                    existing = self._occupations.get(device_id)
                    if (existing is not None
                            and existing.campaign_id == campaign.campaign_id
                            and existing.batch_id == batch.batch_id):
                        rebuilt[device_id] = existing
                    else:
                        rebuilt[device_id] = DeviceOccupation(
                            device_id=device_id,
                            campaign_id=campaign.campaign_id,
                            batch_id=batch.batch_id,
                            acquired_at=0.0)
        recovered = sorted(set(rebuilt) - set(self._occupations))
        released = sorted(set(self._occupations) - set(rebuilt))
        if recovered:
            logger.info("occupations recovered from in-progress batches: %s",
                        recovered)
        if released:
            logger.info("stale occupations dropped (batch terminal): %s",
                        released)
        self._occupations = rebuilt

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

    # ---- device occupations ---------------------------------------------
    def get_occupation(self, device_id: str) -> DeviceOccupation | None:
        return self._occupations.get(device_id)

    def put_occupation(self, occupation: DeviceOccupation) -> None:
        self._occupations[occupation.device_id] = occupation

    def delete_occupation(self, device_id: str) -> None:
        self._occupations.pop(device_id, None)

    def occupations_for_campaign(self, campaign_id: str
                                 ) -> list[DeviceOccupation]:
        return [o for o in self._occupations.values()
                if o.campaign_id == campaign_id]

    # ---- per-device upgrade events --------------------------------------
    def append_upgrade_event(self, event: UpgradeEvent) -> None:
        self._upgrade_events.setdefault(
            event.device_id, []).append(event)

    def list_upgrade_events(self, device_id: str | None = None
                            ) -> list[UpgradeEvent]:
        if device_id is not None:
            return list(self._upgrade_events.get(device_id, []))
        events: list[UpgradeEvent] = []
        for device_events in self._upgrade_events.values():
            events.extend(device_events)
        return events

    def next_event_seq(self, device_id: str) -> int:
        events = self._upgrade_events.get(device_id, [])
        return (events[-1].seq + 1) if events else 1
