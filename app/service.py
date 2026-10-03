"""门面层：聚合鉴权、遥测、升级三个服务。

HTTP / CLI / 测试 / 模拟器都经由本门面，保证各入口的判定结论一致、
原因码一致。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.auth import AuthService
from app.errors import AuthError
from app.models import AuthReason
from app.store import Store
from app.telemetry import TelemetryService
from app.upgrade import UpgradeService


class Gateway:
    def __init__(self, store_path: str | Path | None = None,
                 clock: Callable[[], float] | None = None,
                 quota_window_seconds: float = 60.0,
                 quota_max_per_window: int = 100,
                 credential_ttl_seconds: float = 86400.0):
        self.store = Store(store_path)
        self.auth = AuthService(self.store,
                                ttl_seconds=credential_ttl_seconds,
                                clock=clock)
        self.telemetry = TelemetryService(
            self.store, clock=clock,
            quota_window_seconds=quota_window_seconds,
            quota_max_per_window=quota_max_per_window)
        self.upgrade = UpgradeService(self.store, clock=clock)
        # 重启 / 原地升级恢复：按批次终态语义重建并清理设备占用（幂等）
        self.upgrade.restore_occupations()

    def authenticated_telemetry(self, device_id: str, token: str | None,
                                report: dict) -> dict:
        """先鉴权再处理遥测：鉴权失败抛 AuthError（reason 可区分）。"""
        ok, reason, _ = self.auth.authenticate(device_id, token or "")
        if not ok:
            status = 404 if reason == AuthReason.DEVICE_NOT_REGISTERED else 401
            raise AuthError(reason.value,
                            f"接入被拒绝：{reason.value}",
                            http_status=status)
        return self.telemetry.ingest(device_id, report)
