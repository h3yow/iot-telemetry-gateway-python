"""统一的拒绝原因异常（携带可区分的 reason 码）。

HTTP 层把 GatewayError 映射为 4xx 响应，响应体保留 reason，
调用方可以据此区分「未注册 / 凭据过期 / 被吊销 / 非法格式 / 超限」等。
"""
from __future__ import annotations


class GatewayError(Exception):
    """所有业务拒绝的基类。"""

    reason: str = "error"
    http_status: int = 400

    def __init__(self, reason: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        if http_status is not None:
            self.http_status = http_status

    def to_dict(self) -> dict:
        return {"ok": False, "reason": self.reason, "message": self.message}


class AuthError(GatewayError):
    """接入鉴权拒绝，统一 401。"""
    http_status = 401


class TelemetryError(GatewayError):
    """遥测拒绝：非法格式 422，配额超限 429。"""
    http_status = 422


class NotFoundError(GatewayError):
    http_status = 404


class ConflictError(GatewayError):
    http_status = 409
