"""领域模型：设备、凭据、遥测、固件、升级批次。

全部使用 dataclass，便于序列化到 JSON 持久化文件（重启后状态一致）。
时间一律采用 Unix 时间戳（float，秒），便于模拟与回跳测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class CredentialState(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"  # 过期由 expires_at 判定，不另设状态


class BatchState(str, Enum):
    """升级批次状态机：

    PENDING（待开始）
      --start--> IN_PROGRESS（进行中）
      --全部成功--> SUCCEEDED
      --任一设备异常/健康检查失败--> ROLLING_BACK
      --回滚完成--> ROLLED_BACK（批次内设备版本恢复到升级前版本）
    PENDING 也可直接 cancel -> CANCELLED（尚未开始，不影响设备）
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"


class DeviceUpgradeState(str, Enum):
    """批次内单个设备的升级状态。"""

    PENDING = "PENDING"
    FLASHING = "FLASHING"
    REBOOTING = "REBOOTING"
    HEALTHY = "HEALTHY"          # 健康检查通过
    FAILED = "FAILED"            # 上报异常/健康检查失败
    ROLLED_BACK = "ROLLED_BACK"  # 已回滚到升级前版本


@dataclass
class Credential:
    credential_id: str
    secret: str
    issued_at: float
    expires_at: float
    state: CredentialState = CredentialState.ACTIVE
    revoked_at: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Credential":
        return cls(
            credential_id=d["credential_id"],
            secret=d["secret"],
            issued_at=d["issued_at"],
            expires_at=d["expires_at"],
            state=CredentialState(d.get("state", CredentialState.ACTIVE.value)),
            revoked_at=d.get("revoked_at"),
        )


@dataclass
class Device:
    device_id: str
    current_version: str = "v0"
    # credential_id -> Credential；同一设备可有多把凭据，轮换采用"签发新 + 吊销旧"
    credentials: dict[str, Credential] = field(default_factory=dict)
    registered_at: float = field(default_factory=time.time)
    # 设备当前所在的进行中批次（用于并发升级互斥），None 表示空闲
    active_batch_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "current_version": self.current_version,
            "credentials": {k: c.to_dict() for k, c in self.credentials.items()},
            "registered_at": self.registered_at,
            "active_batch_id": self.active_batch_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Device":
        dev = cls(
            device_id=d["device_id"],
            current_version=d.get("current_version", "v0"),
            registered_at=d["registered_at"],
            active_batch_id=d.get("active_batch_id"),
        )
        dev.credentials = {
            k: Credential.from_dict(v) for k, v in d.get("credentials", {}).items()
        }
        return dev


@dataclass
class TelemetryReport:
    """一次遥测上报。seq 为设备单调递增的上报序号。"""

    device_id: str
    seq: int
    ts: float                     # 设备侧时间戳（可能回跳）
    metrics: dict[str, float]


@dataclass
class TelemetryStats:
    """设备遥测统计：基于 seq 去重，乱序/重复/回跳都安全。"""

    received: int = 0             # 实际受理（首次见到该 seq）的条数
    duplicates: int = 0           # 幂等重放的条数
    rejected: int = 0             # 被拒绝（格式/配额）的条数
    last_seq: int | None = None    # 已受理的最大 seq
    last_ts: float | None = None   # 已受理中的最大设备时间戳（回跳不更新）
    # seq -> 首次受理时的响应结论，重放直接复用 => 幂等
    accepted_seq: dict[int, bool] = field(default_factory=dict)
    # 指标累加：每个 seq 只累加一次
    totals: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TelemetryStats":
        return cls(**d)


@dataclass
class Firmware:
    firmware_id: str
    version: str
    size: int
    checksum: str                 # 仅本地模拟
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Firmware":
        return cls(**d)


@dataclass
class DeviceUpgrade:
    device_id: str
    state: DeviceUpgradeState = DeviceUpgradeState.PENDING
    from_version: str = ""
    to_version: str = ""
    reason: str | None = None     # 失败原因（可解释）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceUpgrade":
        return cls(
            device_id=d["device_id"],
            state=DeviceUpgradeState(d.get("state", DeviceUpgradeState.PENDING.value)),
            from_version=d.get("from_version", ""),
            to_version=d.get("to_version", ""),
            reason=d.get("reason"),
        )


@dataclass
class Batch:
    batch_id: str
    firmware_id: str
    target_version: str
    device_ids: list[str]
    state: BatchState = BatchState.PENDING
    upgrades: dict[str, DeviceUpgrade] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "firmware_id": self.firmware_id,
            "target_version": self.target_version,
            "device_ids": list(self.device_ids),
            "state": self.state.value,
            "upgrades": {k: v.to_dict() for k, v in self.upgrades.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Batch":
        b = cls(
            batch_id=d["batch_id"],
            firmware_id=d["firmware_id"],
            target_version=d["target_version"],
            device_ids=list(d["device_ids"]),
            state=BatchState(d.get("state", BatchState.PENDING.value)),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            failure_reason=d.get("failure_reason"),
        )
        b.upgrades = {
            k: DeviceUpgrade.from_dict(v) for k, v in d.get("upgrades", {}).items()
        }
        return b
