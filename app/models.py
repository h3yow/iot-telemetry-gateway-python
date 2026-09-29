"""领域模型与判定结果枚举。

所有枚举值都是稳定的字符串码，作为「可区分的判定依据」出现在：
- 服务返回值 / HTTP 响应体的 reason 字段
- 单测日志（设备标识、上报序号 + reason）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CredentialStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class AuthReason(str, Enum):
    """接入鉴权判定原因，互相可区分。"""
    OK = "ok"
    DEVICE_NOT_REGISTERED = "device_not_registered"
    CREDENTIAL_NOT_FOUND = "credential_not_found"
    CREDENTIAL_REVOKED = "credential_revoked"
    CREDENTIAL_EXPIRED = "credential_expired"
    SECRET_MISMATCH = "secret_mismatch"


class TelemetryReason(str, Enum):
    """遥测处理判定原因，幂等重放与拒绝互相可区分。"""
    ACCEPTED = "accepted"
    DUPLICATE_REPLAY = "duplicate_replay"
    INVALID_PAYLOAD = "invalid_payload"
    QUOTA_EXCEEDED = "quota_exceeded"
    SEQ_REGRESSED = "seq_regressed"


class BatchState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class CampaignState(str, Enum):
    """活动生命周期：active 可推进；paused 暂停推进；cancelled 终止。"""
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class UpgradeEventType(str, Enum):
    """单台设备升级往来的事件类型，按发生先后追加。"""
    STARTED = "upgrade_started"
    SUCCEEDED = "upgrade_succeeded"
    ROLLED_BACK = "upgrade_rolled_back"
    CANCELLED = "upgrade_cancelled"


class DeviceUpgradeState(str, Enum):
    IDLE = "idle"
    UPGRADING = "upgrading"
    ON_TARGET = "on_target"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


def _enum_value(v: Any) -> Any:
    return v.value if isinstance(v, Enum) else v


@dataclass
class Credential:
    credential_id: str
    device_id: str
    secret_hash: str
    issued_at: float
    expires_at: float
    status: CredentialStatus = CredentialStatus.ACTIVE
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "credential_id": self.credential_id,
            "device_id": self.device_id,
            "secret_hash": self.secret_hash,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "status": _enum_value(self.status),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Credential":
        return cls(
            credential_id=data["credential_id"],
            device_id=data["device_id"],
            secret_hash=data["secret_hash"],
            issued_at=data["issued_at"],
            expires_at=data["expires_at"],
            status=CredentialStatus(data.get("status", "active")),
            version=data.get("version", 1),
        )


@dataclass
class Device:
    device_id: str
    firmware_version: str = "1.0.0"
    registered_at: float = 0.0
    current_credential_id: str | None = None
    upgrade_state: DeviceUpgradeState = DeviceUpgradeState.IDLE
    target_version: str | None = None
    campaign_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "firmware_version": self.firmware_version,
            "registered_at": self.registered_at,
            "current_credential_id": self.current_credential_id,
            "upgrade_state": _enum_value(self.upgrade_state),
            "target_version": self.target_version,
            "campaign_id": self.campaign_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Device":
        return cls(
            device_id=data["device_id"],
            firmware_version=data.get("firmware_version", "1.0.0"),
            registered_at=data.get("registered_at", 0.0),
            current_credential_id=data.get("current_credential_id"),
            upgrade_state=DeviceUpgradeState(
                data.get("upgrade_state", "idle")),
            target_version=data.get("target_version"),
            campaign_id=data.get("campaign_id"),
        )


@dataclass
class TelemetryRecord:
    seq: int
    ts: float
    received_at: float
    payload: dict

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts,
                "received_at": self.received_at, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict) -> "TelemetryRecord":
        return cls(seq=data["seq"], ts=data["ts"],
                   received_at=data["received_at"], payload=data["payload"])


@dataclass
class TelemetryStats:
    device_id: str
    accepted_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    max_seq: int | None = None
    max_ts: float | None = None
    seen_seqs: set[int] = field(default_factory=set)
    records: list[TelemetryRecord] = field(default_factory=list)
    quota_used_windows: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "accepted_count": self.accepted_count,
            "duplicate_count": self.duplicate_count,
            "rejected_count": self.rejected_count,
            "max_seq": self.max_seq,
            "max_ts": self.max_ts,
            "seen_seqs": sorted(self.seen_seqs),
            "records": [r.to_dict() for r in self.records],
            "quota_used_windows": self.quota_used_windows,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TelemetryStats":
        return cls(
            device_id=data["device_id"],
            accepted_count=data.get("accepted_count", 0),
            duplicate_count=data.get("duplicate_count", 0),
            rejected_count=data.get("rejected_count", 0),
            max_seq=data.get("max_seq"),
            max_ts=data.get("max_ts"),
            seen_seqs=set(data.get("seen_seqs", [])),
            records=[TelemetryRecord.from_dict(r)
                     for r in data.get("records", [])],
            quota_used_windows=dict(data.get("quota_used_windows", {})),
        )


@dataclass
class FirmwareImage:
    version: str
    checksum: str
    size: int
    created_at: float = 0.0
    released: bool = False

    def to_dict(self) -> dict:
        return {"version": self.version, "checksum": self.checksum,
                "size": self.size, "created_at": self.created_at,
                "released": self.released}

    @classmethod
    def from_dict(cls, data: dict) -> "FirmwareImage":
        return cls(version=data["version"], checksum=data["checksum"],
                   size=data["size"], created_at=data.get("created_at", 0.0),
                   released=data.get("released", False))


@dataclass
class DeviceUpgrade:
    device_id: str
    from_version: str
    to_version: str
    state: DeviceUpgradeState
    healthy: bool | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "state": _enum_value(self.state),
            "healthy": self.healthy,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceUpgrade":
        return cls(
            device_id=data["device_id"],
            from_version=data["from_version"],
            to_version=data["to_version"],
            state=DeviceUpgradeState(data["state"]),
            healthy=data.get("healthy"),
            message=data.get("message", ""),
        )


@dataclass
class DeviceOccupation:
    """设备占用关系：同一时刻一台设备最多被一个活动的进行中批次占用。"""
    device_id: str
    campaign_id: str
    batch_id: str
    acquired_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "campaign_id": self.campaign_id,
            "batch_id": self.batch_id,
            "acquired_at": self.acquired_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceOccupation":
        return cls(
            device_id=data["device_id"],
            campaign_id=data["campaign_id"],
            batch_id=data["batch_id"],
            acquired_at=data.get("acquired_at", 0.0),
        )


@dataclass
class UpgradeEvent:
    """单台设备的一次升级往来记录，顺序追加，重启后仍在。"""
    seq: int
    device_id: str
    campaign_id: str
    batch_id: str
    event_type: UpgradeEventType
    from_version: str
    to_version: str
    at: float = 0.0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "device_id": self.device_id,
            "campaign_id": self.campaign_id,
            "batch_id": self.batch_id,
            "event_type": _enum_value(self.event_type),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "at": self.at,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UpgradeEvent":
        return cls(
            seq=data["seq"],
            device_id=data["device_id"],
            campaign_id=data["campaign_id"],
            batch_id=data["batch_id"],
            event_type=UpgradeEventType(data["event_type"]),
            from_version=data["from_version"],
            to_version=data["to_version"],
            at=data.get("at", 0.0),
            message=data.get("message", ""),
        )


@dataclass
class Batch:
    batch_id: str
    campaign_id: str
    device_ids: list[str]
    order: int
    state: BatchState = BatchState.PENDING
    upgrades: dict[str, DeviceUpgrade] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "campaign_id": self.campaign_id,
            "device_ids": list(self.device_ids),
            "order": self.order,
            "state": _enum_value(self.state),
            "upgrades": {k: v.to_dict() for k, v in self.upgrades.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Batch":
        return cls(
            batch_id=data["batch_id"],
            campaign_id=data["campaign_id"],
            device_ids=list(data["device_ids"]),
            order=data["order"],
            state=BatchState(data.get("state", "pending")),
            upgrades={k: DeviceUpgrade.from_dict(v)
                      for k, v in data.get("upgrades", {}).items()},
        )


@dataclass
class Campaign:
    campaign_id: str
    firmware_version: str
    batches: list[Batch] = field(default_factory=list)
    created_at: float = 0.0
    state: CampaignState = CampaignState.ACTIVE

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "firmware_version": self.firmware_version,
            "batches": [b.to_dict() for b in self.batches],
            "created_at": self.created_at,
            "state": _enum_value(self.state),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Campaign":
        return cls(
            campaign_id=data["campaign_id"],
            firmware_version=data["firmware_version"],
            batches=[Batch.from_dict(b) for b in data.get("batches", [])],
            created_at=data.get("created_at", 0.0),
            state=CampaignState(data.get("state", "active")),
        )
