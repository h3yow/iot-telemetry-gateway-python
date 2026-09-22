"""FastAPI HTTP 入口。

所有业务结论均来自 :class:`GatewayService`；``GatewayError`` 统一映射为
带稳定错误码的 JSON 响应，便于调用方区分拒绝原因。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .errors import ErrorCode, GatewayError
from .service import GatewayService

# 错误码 -> HTTP 状态（错误码本身才是区分拒绝原因的稳定依据）
_STATUS = {
    ErrorCode.CREDENTIAL_MISSING: 401,
    ErrorCode.CREDENTIAL_NOT_FOUND: 401,
    ErrorCode.CREDENTIAL_REVOKED: 401,
    ErrorCode.CREDENTIAL_EXPIRED: 401,
    ErrorCode.DEVICE_NOT_FOUND: 404,
    ErrorCode.FIRMWARE_NOT_FOUND: 404,
    ErrorCode.BATCH_NOT_FOUND: 404,
    ErrorCode.ROLLOUT_NOT_FOUND: 404,
    ErrorCode.DEVICE_EXISTS: 409,
    ErrorCode.FIRMWARE_EXISTS: 409,
    ErrorCode.BATCH_NOT_ACTIVE: 409,
    ErrorCode.DEVICE_BUSY: 409,
    ErrorCode.TELEMETRY_QUOTA_EXCEEDED: 429,
    ErrorCode.TELEMETRY_BAD_FORMAT: 400,
    ErrorCode.BATCH_DEVICE_NOT_IN: 422,
}


def create_app(service: GatewayService | None = None) -> FastAPI:
    if service is None:
        service = GatewayService(os.environ.get("GATEWAY_STATE_FILE"))
    app = FastAPI(title="IoT 遥测接入与固件灰度升级网关（本地模拟）")
    app.state.service = service

    @app.exception_handler(GatewayError)
    async def _on_gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=_STATUS.get(exc.code, 400),
                            content=exc.to_dict())

    @app.get("/")
    def read_root() -> dict:
        return {"service": "iot-telemetry-gateway", "mode": "local-simulation"}

    # ---------- 设备 ----------
    @app.post("/devices", status_code=201)
    def register_device(body: dict[str, Any]) -> dict:
        return service.register_device(body["device_id"])

    @app.get("/devices/{device_id}")
    def get_device(device_id: str) -> dict:
        return service.device_view(device_id)

    @app.post("/devices/{device_id}/credentials")
    def issue_credential(device_id: str, body: dict[str, Any] | None = None) -> dict:
        ttl = (body or {}).get("ttl_seconds", 3600.0)
        return service.issue_credential(device_id, ttl)

    @app.post("/devices/{device_id}/credentials/rotate")
    def rotate_credentials(device_id: str, body: dict[str, Any] | None = None) -> dict:
        ttl = (body or {}).get("ttl_seconds", 3600.0)
        return service.rotate_credentials(device_id, ttl)

    @app.post("/devices/{device_id}/credentials/{credential_id}/revoke")
    def revoke_credential(device_id: str, credential_id: str) -> dict:
        return service.revoke_credential(device_id, credential_id)

    # ---------- 遥测 ----------
    @app.post("/devices/{device_id}/telemetry")
    async def ingest_telemetry(
        device_id: str,
        request: Request,
        x_credential_id: str | None = Header(default=None),
        x_credential_secret: str | None = Header(default=None),
    ) -> dict:
        payload = await request.json()
        return service.ingest_telemetry(
            device_id, x_credential_id, x_credential_secret, payload
        )

    @app.get("/devices/{device_id}/stats")
    def get_stats(device_id: str) -> dict:
        return service.get_stats(device_id)

    # ---------- 固件 ----------
    @app.post("/firmware", status_code=201)
    def add_firmware(body: dict[str, Any]) -> dict:
        return service.add_firmware(
            body["firmware_id"], body["version"],
            int(body["size"]), str(body["checksum"]),
        )

    # ---------- 灰度升级 ----------
    @app.post("/rollouts", status_code=201)
    def create_rollout(body: dict[str, Any]) -> dict:
        return service.create_rollout(
            body["firmware_id"], list(body["device_ids"]),
            int(body.get("batch_size", 2)),
        )

    @app.get("/batches")
    def list_batches() -> dict:
        return {"batches": service.list_batches()}

    @app.get("/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict:
        return service.get_batch(batch_id)

    @app.post("/batches/{batch_id}/start")
    def start_batch(batch_id: str) -> dict:
        return service.start_batch(batch_id)

    @app.post("/batches/{batch_id}/report")
    def report_device(batch_id: str, body: dict[str, Any]) -> dict:
        return service.report_device(
            batch_id, body["device_id"], bool(body["success"]),
            body.get("message"),
        )

    @app.post("/batches/{batch_id}/health")
    def health_check(batch_id: str, body: dict[str, Any]) -> dict:
        return service.health_check(
            batch_id, body["device_id"], bool(body["healthy"])
        )

    @app.post("/batches/{batch_id}/rollback")
    def rollback_batch(batch_id: str, body: dict[str, Any] | None = None) -> dict:
        return service.rollback_batch(batch_id, (body or {}).get("reason"))

    return app
