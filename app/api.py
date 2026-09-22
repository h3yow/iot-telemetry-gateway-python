"""FastAPI 路由：HTTP 入口，判定全部委托给门面 Gateway。

响应约定：
- 成功：{"ok": true, ...}
- 拒绝：{"ok": false, "reason": <可区分原因码>, "message": ...}
  401 鉴权拒绝（未注册为 404）/ 422 非法格式 / 429 配额超限 /
  404 资源不存在 / 409 状态冲突。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from app.errors import GatewayError
from app.models import AuthReason

router = APIRouter(prefix="/api/v1")


def get_gateway(request: Request):
    return request.app.state.gateway


def _raise(exc: GatewayError) -> None:
    raise HTTPException(status_code=exc.http_status, detail=exc.to_dict())


def _auth_or_401(gateway, device_id: str, authorization: str | None) -> None:
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
    ok, reason, _ = gateway.auth.authenticate(device_id, token)
    if not ok:
        status = 404 if reason == AuthReason.DEVICE_NOT_REGISTERED else 401
        raise HTTPException(
            status_code=status,
            detail={"ok": False, "reason": reason.value,
                    "message": f"接入被拒绝：{reason.value}"})


def _body(request: Request) -> dict:
    return dict(request.state.json_body)


@router.post("/devices/{device_id}/register")
def register_device(device_id: str, request: Request):
    body = request.state.json_body
    try:
        device = get_gateway(request).auth.register_device(
            device_id, firmware_version=body.get("firmware_version", "1.0.0"))
    except GatewayError as exc:
        _raise(exc)
    return {"ok": True, "device": device.to_dict()}


@router.post("/devices/{device_id}/credentials")
def issue_credential(device_id: str, request: Request):
    try:
        issued = get_gateway(request).auth.issue_credential(device_id)
    except GatewayError as exc:
        _raise(exc)
    return {"ok": True, **issued}


@router.post("/devices/{device_id}/credentials/{credential_id}/revoke")
def revoke_credential(device_id: str, credential_id: str, request: Request):
    try:
        cred = get_gateway(request).auth.revoke_credential(credential_id)
    except GatewayError as exc:
        _raise(exc)
    if cred.device_id != device_id:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "reason": "credential_not_found",
                    "message": "凭据不属于该设备"})
    return {"ok": True, "credential": cred.to_dict()}


@router.post("/devices/{device_id}/telemetry")
async def ingest_telemetry(device_id: str, request: Request,
                           authorization: str | None = Header(default=None)):
    gateway = get_gateway(request)
    _auth_or_401(gateway, device_id, authorization)
    report = request.state.json_body
    try:
        result = gateway.telemetry.ingest(device_id, report)
    except GatewayError as exc:
        _raise(exc)
    return {"ok": True, **result}


@router.get("/devices/{device_id}/telemetry")
def get_telemetry(device_id: str, request: Request,
                  authorization: str | None = Header(default=None)):
    gateway = get_gateway(request)
    _auth_or_401(gateway, device_id, authorization)
    return {"ok": True, "stats": gateway.telemetry.stats(device_id)}


@router.post("/firmware")
async def release_firmware(request: Request):
    body = request.state.json_body
    try:
        fw = get_gateway(request).upgrade.release_firmware(
            body["version"], size=int(body.get("size", 1024)))
    except GatewayError as exc:
        _raise(exc)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "reason": "invalid_payload",
                    "message": "version 缺失或非法"})
    return {"ok": True, "firmware": fw.to_dict()}


@router.post("/campaigns")
async def create_campaign(request: Request):
    body = request.state.json_body
    try:
        campaign = get_gateway(request).upgrade.create_campaign(
            body["firmware_version"], list(body["device_ids"]),
            batch_size=int(body.get("batch_size", 2)))
    except GatewayError as exc:
        _raise(exc)
    except (KeyError, TypeError):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "reason": "invalid_payload",
                    "message": "firmware_version/device_ids 缺失或非法"})
    return {"ok": True, "campaign": campaign.to_dict()}


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request):
    try:
        progress = get_gateway(request).upgrade.campaign_progress(campaign_id)
    except GatewayError as exc:
        _raise(exc)
    return {"ok": True, **progress}


@router.post("/campaigns/{campaign_id}/batches/{batch_id}/upgrade")
async def upgrade_batch(campaign_id: str, batch_id: str, request: Request):
    body = request.state.json_body
    health_results = body.get("health_results")
    try:
        batch = get_gateway(request).upgrade.upgrade_batch(
            campaign_id, batch_id, health_results=health_results)
    except GatewayError as exc:
        _raise(exc)
    return {"ok": True, "batch": batch}


@router.post("/campaigns/{campaign_id}/batches/{batch_id}/report")
async def report_device(campaign_id: str, batch_id: str, request: Request):
    body = request.state.json_body
    try:
        batch = get_gateway(request).upgrade.report_device(
            campaign_id, batch_id, body["device_id"],
            ok=bool(body["ok"]), message=str(body.get("message", "")))
    except GatewayError as exc:
        _raise(exc)
    except (KeyError, TypeError):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "reason": "invalid_payload",
                    "message": "device_id/ok 缺失或非法"})
    return {"ok": True, "batch": batch}


@router.post("/campaigns/{campaign_id}/batches/{batch_id}/rollback")
def rollback_batch(campaign_id: str, batch_id: str, request: Request):
    try:
        batch = get_gateway(request).upgrade.rollback_batch(
            campaign_id, batch_id)
    except GatewayError as exc:
        _raise(exc)
    return {"ok": True, "batch": batch}


@router.get("/devices")
def list_devices(request: Request):
    return {"ok": True,
            "devices": [d.to_dict()
                        for d in get_gateway(request).store.list_devices()]}


@router.get("/devices/{device_id}")
def get_device(device_id: str, request: Request):
    device = get_gateway(request).store.get_device(device_id)
    if device is None:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "reason": "device_not_registered",
                    "message": f"设备 {device_id} 未注册"})
    return {"ok": True, "device": device.to_dict()}


def create_app(gateway=None) -> "FastAPI":
    """构建 FastAPI 应用（含 JSON body 中间件），主入口与测试共用。"""
    from app.service import Gateway
    from fastapi import FastAPI
    from fastapi.requests import Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="IoT 遥测接入与固件灰度升级网关（本地模拟）")
    app.state.gateway = gateway if gateway is not None else Gateway()

    @app.middleware("http")
    async def parse_json_body(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            raw = await request.body()
            if not raw.strip():
                request.state.json_body = {}
                return await call_next(request)
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    status_code=422,
                    content={"ok": False, "reason": "invalid_payload",
                             "message": "请求体不是合法 JSON"})
            if not isinstance(body, dict):
                return JSONResponse(
                    status_code=422,
                    content={"ok": False, "reason": "invalid_payload",
                             "message": "请求体必须是 JSON 对象"})
            request.state.json_body = body
        else:
            request.state.json_body = {}
        return await call_next(request)

    app.include_router(router)

    @app.get("/")
    def read_root():
        return {"service": "iot-telemetry-gateway", "status": "ok"}

    return app
