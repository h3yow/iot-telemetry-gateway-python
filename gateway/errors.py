"""可区分的拒绝原因（错误码）与异常类型。

所有业务拒绝都抛出 :class:`GatewayError`，携带稳定的错误码，
HTTP 入口据此返回 4xx，CLI 入口据此打印同一结论，保证"各入口结论一致"。
"""

from __future__ import annotations


class ErrorCode:
    # ---- 注册 / 鉴权 ----
    DEVICE_EXISTS = "DEVICE_EXISTS"                # 设备已注册
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"          # 设备未注册
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"      # 未携带凭据
    CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"  # 凭据不存在/已轮换删除
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"      # 凭据被吊销
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"      # 凭据已过期

    # ---- 遥测 ----
    TELEMETRY_BAD_FORMAT = "TELEMETRY_BAD_FORMAT"  # 格式非法
    TELEMETRY_QUOTA_EXCEEDED = "TELEMETRY_QUOTA_EXCEEDED"  # 超出配额

    # ---- 固件 / 升级 ----
    FIRMWARE_NOT_FOUND = "FIRMWARE_NOT_FOUND"
    FIRMWARE_EXISTS = "FIRMWARE_EXISTS"
    BATCH_NOT_FOUND = "BATCH_NOT_FOUND"
    BATCH_NOT_ACTIVE = "BATCH_NOT_ACTIVE"
    BATCH_DEVICE_NOT_IN = "BATCH_DEVICE_NOT_IN"
    DEVICE_BUSY = "DEVICE_BUSY"                    # 设备已在另一进行中的批次
    ROLLOUT_NOT_FOUND = "ROLLOUT_NOT_FOUND"


class GatewayError(Exception):
    """带稳定错误码的业务异常。"""

    def __init__(self, code: str, message: str, *, device_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.device_id = device_id

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.device_id is not None:
            d["device_id"] = self.device_id
        return d
