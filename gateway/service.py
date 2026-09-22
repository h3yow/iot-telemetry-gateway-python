"""网关应用服务。

HTTP（FastAPI）与 CLI（scripts/demo.py）共用本类：
- 每次变更在同一把进程级 RLock 内完成并原子落盘，保证并发不互相覆盖、
  重启后升级状态一致；
- 鉴权、遥测判定、升级状态机全部复用领域模块，两个入口结论一致。
"""

from __future__ import annotations

import time
from typing import Any

from . import credentials as cred_mod
from . import telemetry as tel_mod
from .devices import DeviceRegistry
from .errors import ErrorCode, GatewayError
from .firmware import FirmwareCatalog
from .models import Batch, Credential, Device, Firmware, TelemetryStats
from .store import JsonStore
from .upgrades import UpgradeManager

STATE_VERSION = 1


class GatewayService:
    def __init__(
        self,
        store_path: str | None = None,
        *,
        telemetry_quota: int = 100,
        now_func=time.time,
    ) -> None:
        self.store = JsonStore(store_path)
        self.now = now_func
        self.quota = telemetry_quota
        self.devices = DeviceRegistry()
        self.catalog = FirmwareCatalog()
        self.stats: dict[str, TelemetryStats] = {}
        self.upgrades = UpgradeManager(self.devices, self.catalog, now_func=now_func)
        self._restore(self.store.load())

    # ================= 设备注册与凭据 =================
    def register_device(self, device_id: str) -> dict[str, Any]:
        with self.store.lock:
            device = self.devices.register(device_id)
            cred = cred_mod.issue_credential(device, now=self.now())
            self._persist()
            return self._device_view(device, cred)

    def issue_credential(self, device_id: str, ttl_seconds: float = 3600.0) -> dict:
        with self.store.lock:
            device = self.devices.get(device_id)
            cred = cred_mod.issue_credential(
                device, ttl_seconds=ttl_seconds, now=self.now()
            )
            self._persist()
            return self._cred_view(device, cred)

    def rotate_credentials(self, device_id: str, ttl_seconds: float = 3600.0) -> dict:
        with self.store.lock:
            device = self.devices.get(device_id)
            cred = cred_mod.rotate_credentials(
                device, ttl_seconds=ttl_seconds, now=self.now()
            )
            self._persist()
            return self._cred_view(device, cred)

    def revoke_credential(self, device_id: str, credential_id: str) -> dict:
        with self.store.lock:
            device = self.devices.get(device_id)
            cred_mod.revoke_credential(device, credential_id, now=self.now())
            self._persist()
            return {"device_id": device_id, "credential_id": credential_id,
                    "state": "REVOKED"}

    def authenticate(self, device_id: str, credential_id: str | None,
                     secret: str | None) -> Device:
        with self.store.lock:
            device = self.devices.get(device_id)
            return cred_mod.authenticate(
                device, credential_id, secret, now=self.now()
            )

    # ================= 遥测 =================
    def ingest_telemetry(
        self,
        device_id: str,
        credential_id: str | None,
        secret: str | None,
        raw_payload: Any,
    ) -> dict[str, Any]:
        with self.store.lock:
            # 1) 先鉴权（任何拒绝原因都通过 GatewayError.code 区分）
            device = self.devices.get(device_id)
            cred_mod.authenticate(device, credential_id, secret, now=self.now())

            # 2) 格式校验（非法抛 TELEMETRY_BAD_FORMAT）
            report = tel_mod.validate_payload(device_id, raw_payload)

            # 3) 幂等处理
            stats = self.stats.setdefault(device_id, TelemetryStats())
            result = tel_mod.process_report(
                stats, report, quota=self.quota, now=self.now()
            )
            self._persist()
            # 配额拒绝属于"被拒绝"：service 层转为错误码（HTTP 429）；
            # 幂等重放（DUPLICATE）仍然成功返回，二者在入口层也可区分。
            if result["reason"] == tel_mod.REJECTED_QUOTA:
                raise GatewayError(
                    ErrorCode.TELEMETRY_QUOTA_EXCEEDED,
                    result["detail"], device_id=device_id,
                )
            result["stats"] = stats.to_dict()
            return result

    def get_stats(self, device_id: str) -> dict:
        with self.store.lock:
            self.devices.get(device_id)
            return self.stats.get(device_id, TelemetryStats()).to_dict()

    # ================= 固件与升级 =================
    def add_firmware(self, firmware_id: str, version: str, size: int,
                     checksum: str) -> dict:
        with self.store.lock:
            fw = Firmware(
                firmware_id=firmware_id, version=version,
                size=size, checksum=checksum,
            )
            self.catalog.add(fw)
            self._persist()
            return fw.to_dict()

    def create_rollout(self, firmware_id: str, device_ids: list[str],
                       batch_size: int = 2) -> dict:
        with self.store.lock:
            rollout_id, batches = self.upgrades.create_rollout(
                firmware_id, device_ids, batch_size
            )
            self._persist()
            return {"rollout_id": rollout_id,
                    "batches": [b.batch_id for b in batches]}

    def start_batch(self, batch_id: str) -> dict:
        with self.store.lock:
            b = self.upgrades.start_batch(batch_id)
            self._persist()
            return b.to_dict()

    def report_device(self, batch_id: str, device_id: str, success: bool,
                      message: str | None = None) -> dict:
        with self.store.lock:
            b = self.upgrades.report_device(batch_id, device_id, success, message)
            self._persist()
            return b.to_dict()

    def health_check(self, batch_id: str, device_id: str, healthy: bool) -> dict:
        with self.store.lock:
            b = self.upgrades.health_check(batch_id, device_id, healthy)
            self._persist()
            return b.to_dict()

    def rollback_batch(self, batch_id: str, reason: str | None = None) -> dict:
        with self.store.lock:
            b = self.upgrades.rollback_batch(batch_id, reason)
            self._persist()
            return b.to_dict()

    def get_batch(self, batch_id: str) -> dict:
        with self.store.lock:
            return self.upgrades.get_batch(batch_id).to_dict()

    def list_batches(self) -> list[dict]:
        with self.store.lock:
            return [b.to_dict() for b in self.upgrades.list_batches()]

    def device_view(self, device_id: str) -> dict:
        with self.store.lock:
            d = self.devices.get(device_id)
            return {
                "device_id": d.device_id,
                "current_version": d.current_version,
                "active_batch_id": d.active_batch_id,
                "credentials": [
                    {"credential_id": c.credential_id, "state": c.state.value,
                     "expires_at": c.expires_at}
                    for c in d.credentials.values()
                ],
            }

    # ================= 持久化 =================
    def _snapshot(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "devices": {d.device_id: d.to_dict() for d in self.devices.all()},
            "firmware": {f.firmware_id: f.to_dict() for f in self.catalog.all()},
            "stats": {k: v.to_dict() for k, v in self.stats.items()},
            "batches": {b.batch_id: b.to_dict()
                        for b in self.upgrades.list_batches()},
            "rollouts": dict(self.upgrades._rollouts),
        }

    def _persist(self) -> None:
        self.store.save(self._snapshot())

    def _restore(self, data: dict[str, Any]) -> None:
        if not data:
            return
        for did, dd in data.get("devices", {}).items():
            self.devices._devices[did] = Device.from_dict(dd)
        for fid, fd in data.get("firmware", {}).items():
            self.catalog._items[fid] = Firmware.from_dict(fd)
        for did, sd in data.get("stats", {}).items():
            self.stats[did] = TelemetryStats.from_dict(sd)
        mgr = self.upgrades
        for bid, bd in data.get("batches", {}).items():
            mgr._batches[bid] = Batch.from_dict(bd)
        mgr._rollouts = {k: list(v) for k, v in data.get("rollouts", {}).items()}

    # ================= 视图辅助 =================
    @staticmethod
    def _cred_view(device: Device, cred: Credential) -> dict[str, Any]:
        return {
            "device_id": device.device_id,
            "credential_id": cred.credential_id,
            "secret": cred.secret,
            "issued_at": cred.issued_at,
            "expires_at": cred.expires_at,
        }

    def _device_view(self, device: Device, cred: Credential) -> dict[str, Any]:
        view = self._cred_view(device, cred)
        view["current_version"] = device.current_version
        return view
