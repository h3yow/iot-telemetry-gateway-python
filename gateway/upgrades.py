"""固件分批灰度升级。

批次状态机见 ``models.BatchState``；设备子状态见 ``DeviceUpgradeState``。

关键一致性保证（领域层，进程级互斥由 service 持锁保证）：
- 设备版本只在整批 SUCCEEDED 时提交一次，失败路径从不改版本，
  因此回滚等价于"版本保持升级前值 + 设备子状态置 ROLLED_BACK"，
  回滚严格限定在本批次设备，不影响其他批次。
- 设备创建批次时即被占用（device.active_batch_id），
  并发的第二个升级请求收到 DEVICE_BUSY，进度与版本不会互相覆盖。
- 所有状态迁移都做前置校验，重复上报/重复检查幂等返回当前状态。
"""

from __future__ import annotations

import secrets as _secrets
import time

from .errors import ErrorCode, GatewayError
from .devices import DeviceRegistry
from .firmware import FirmwareCatalog
from .models import (
    Batch,
    BatchState,
    DeviceUpgrade,
    DeviceUpgradeState,
)

_TERMINAL = {
    BatchState.SUCCEEDED,
    BatchState.ROLLED_BACK,
    BatchState.CANCELLED,
}
_FLASH_DONE = {DeviceUpgradeState.REBOOTING, DeviceUpgradeState.HEALTHY}


class UpgradeManager:
    def __init__(
        self,
        devices: DeviceRegistry,
        catalog: FirmwareCatalog,
        *,
        now_func=time.time,
    ) -> None:
        self._devices = devices
        self._catalog = catalog
        self._batches: dict[str, Batch] = {}
        self._rollouts: dict[str, list[str]] = {}
        self._now = now_func

    # ---------- 查询 ----------
    def get_batch(self, batch_id: str) -> Batch:
        b = self._batches.get(batch_id)
        if b is None:
            raise GatewayError(ErrorCode.BATCH_NOT_FOUND, f"批次 {batch_id} 不存在")
        return b

    def list_batches(self) -> list[Batch]:
        return list(self._batches.values())

    def get_rollout(self, rollout_id: str) -> list[Batch]:
        ids = self._rollouts.get(rollout_id)
        if ids is None:
            raise GatewayError(ErrorCode.ROLLOUT_NOT_FOUND,
                               f"升级任务 {rollout_id} 不存在")
        return [self._batches[i] for i in ids]

    # ---------- 创建灰度任务（分批） ----------
    def create_rollout(
        self, firmware_id: str, device_ids: list[str], batch_size: int = 2
    ) -> tuple[str, list[Batch]]:
        if not device_ids:
            raise GatewayError(ErrorCode.TELEMETRY_BAD_FORMAT, "设备列表不能为空")
        if batch_size <= 0:
            raise GatewayError(ErrorCode.TELEMETRY_BAD_FORMAT, "batch_size 必须为正整数")
        fw = self._catalog.get(firmware_id)  # 固件不存在抛 FIRMWARE_NOT_FOUND

        # 去重，保持顺序
        seen: set[str] = set()
        ordered: list[str] = []
        for did in device_ids:
            dev = self._devices.get(did)  # 未注册抛 DEVICE_NOT_FOUND
            if did in seen:
                continue
            seen.add(did)
            ordered.append(did)
            if dev.active_batch_id is not None:
                raise GatewayError(
                    ErrorCode.DEVICE_BUSY,
                    f"设备 {did} 已在进行中的批次 {dev.active_batch_id}，拒绝并发升级",
                    device_id=did,
                )

        rollout_id = "rollout-" + _secrets.token_hex(4)
        batches: list[Batch] = []
        for start in range(0, len(ordered), batch_size):
            members = ordered[start:start + batch_size]
            batch = Batch(
                batch_id="batch-" + _secrets.token_hex(4),
                firmware_id=fw.firmware_id,
                target_version=fw.version,
                device_ids=members,
            )
            for did in members:
                dev = self._devices.get(did)
                batch.upgrades[did] = DeviceUpgrade(
                    device_id=did,
                    from_version=dev.current_version,
                    to_version=fw.version,
                )
                dev.active_batch_id = batch.batch_id  # 占用设备
            self._batches[batch.batch_id] = batch
            batches.append(batch)
        self._rollouts[rollout_id] = [b.batch_id for b in batches]
        return rollout_id, batches

    # ---------- 批次推进 ----------
    def start_batch(self, batch_id: str) -> Batch:
        b = self.get_batch(batch_id)
        if b.state is not BatchState.PENDING:
            raise GatewayError(
                ErrorCode.BATCH_NOT_ACTIVE,
                f"批次 {batch_id} 状态为 {b.state.value}，无法开始",
            )
        b.state = BatchState.IN_PROGRESS
        b.updated_at = self._now()
        return b

    def report_device(
        self, batch_id: str, device_id: str, success: bool,
        message: str | None = None,
    ) -> Batch:
        """设备上报刷机/重启结果。success=False 立即触发整批回滚。"""
        b = self._require_in_progress(batch_id, device_id)
        up = b.upgrades[device_id]

        if up.state in _FLASH_DONE:
            # 重复上报：幂等，不覆盖既有进度
            return b
        if up.state is DeviceUpgradeState.ROLLED_BACK:
            return b

        if not success:
            up.state = DeviceUpgradeState.FAILED
            up.reason = message or "设备上报升级失败"
            return self._rollback(b, up.reason)

        up.state = DeviceUpgradeState.REBOOTING
        up.reason = message
        b.updated_at = self._now()
        return b

    def health_check(self, batch_id: str, device_id: str, healthy: bool) -> Batch:
        """健康检查：不通过触发整批回滚；全部通过则批次成功并提交版本。"""
        b = self._require_in_progress(batch_id, device_id)
        up = b.upgrades[device_id]

        if up.state is DeviceUpgradeState.HEALTHY:
            return b  # 重复健康检查，幂等
        if up.state is DeviceUpgradeState.ROLLED_BACK:
            return b
        if up.state is not DeviceUpgradeState.REBOOTING:
            raise GatewayError(
                ErrorCode.BATCH_NOT_ACTIVE,
                f"设备 {device_id} 状态为 {up.state.value}，尚不能做健康检查",
                device_id=device_id,
            )

        if not healthy:
            up.state = DeviceUpgradeState.FAILED
            up.reason = "健康检查失败"
            return self._rollback(b, up.reason)

        up.state = DeviceUpgradeState.HEALTHY
        up.reason = None
        b.updated_at = self._now()

        if all(
            u.state is DeviceUpgradeState.HEALTHY for u in b.upgrades.values()
        ):
            self._commit_success(b)
        return b

    def rollback_batch(self, batch_id: str, reason: str | None = None) -> Batch:
        """人工触发整批回滚（仅 IN_PROGRESS / PENDING 可回滚）。"""
        b = self.get_batch(batch_id)
        if b.state in _TERMINAL:
            raise GatewayError(
                ErrorCode.BATCH_NOT_ACTIVE,
                f"批次 {batch_id} 已处于终态 {b.state.value}，不能回滚",
            )
        return self._rollback(b, reason or "人工触发回滚")

    def cancel_pending_batch(self, batch_id: str) -> Batch:
        b = self.get_batch(batch_id)
        if b.state is not BatchState.PENDING:
            raise GatewayError(
                ErrorCode.BATCH_NOT_ACTIVE,
                f"批次 {batch_id} 不是 PENDING，无法取消",
            )
        b.state = BatchState.CANCELLED
        b.updated_at = self._now()
        for did in b.device_ids:
            self._devices.get(did).active_batch_id = None
        return b

    # ---------- 内部 ----------
    def _require_in_progress(self, batch_id: str, device_id: str) -> Batch:
        b = self.get_batch(batch_id)
        if b.state is not BatchState.IN_PROGRESS:
            raise GatewayError(
                ErrorCode.BATCH_NOT_ACTIVE,
                f"批次 {batch_id} 状态为 {b.state.value}，不接受设备上报",
            )
        if device_id not in b.upgrades:
            raise GatewayError(
                ErrorCode.BATCH_DEVICE_NOT_IN,
                f"设备 {device_id} 不属于批次 {batch_id}",
                device_id=device_id,
            )
        return b

    def _commit_success(self, b: Batch) -> None:
        """整批成功：版本只在此处提交一次，并释放设备占用。"""
        for did, up in b.upgrades.items():
            dev = self._devices.get(did)
            dev.current_version = b.target_version  # 单一写点
            dev.active_batch_id = None
        b.state = BatchState.SUCCEEDED
        b.failure_reason = None
        b.updated_at = self._now()

    def _rollback(self, b: Batch, reason: str) -> Batch:
        """整批回滚：仅处理本批次设备；版本未在失败路径提交，故保持原值。"""
        b.state = BatchState.ROLLING_BACK
        b.failure_reason = reason
        b.updated_at = self._now()
        for did, up in b.upgrades.items():
            dev = self._devices.get(did)
            if up.state is not DeviceUpgradeState.PENDING:
                up.state = DeviceUpgradeState.ROLLED_BACK
                up.reason = reason if up.reason is None else up.reason
            else:
                # 尚未开始刷机的设备：标记因回滚跳过，版本无需变动
                up.state = DeviceUpgradeState.ROLLED_BACK
                up.reason = "批次回滚，未执行升级"
            # 关键：显式恢复到升级前版本（正常路径下本就未改变，双保险）
            dev.current_version = up.from_version
            dev.active_batch_id = None
        b.state = BatchState.ROLLED_BACK
        b.updated_at = self._now()
        return b
