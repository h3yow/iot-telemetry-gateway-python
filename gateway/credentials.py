"""凭据签发、轮换、吊销与鉴权（纯领域逻辑，不持久化，由 service 落盘）。"""

from __future__ import annotations

import secrets as _secrets
import time

from .errors import ErrorCode, GatewayError
from .models import Credential, CredentialState, Device


def issue_credential(
    device: Device, *, ttl_seconds: float = 3600.0, now: float | None = None
) -> Credential:
    """为设备签发一把新的 ACTIVE 凭据（不影响旧凭据）。"""
    now = time.time() if now is None else now
    cred = Credential(
        credential_id="cred-" + _secrets.token_hex(8),
        secret=_secrets.token_hex(24),
        issued_at=now,
        expires_at=now + ttl_seconds,
    )
    device.credentials[cred.credential_id] = cred
    return cred


def rotate_credentials(
    device: Device, *, ttl_seconds: float = 3600.0, now: float | None = None
) -> Credential:
    """凭据轮换：签发新凭据并立即吊销所有现存凭据。

    旧凭据在本调用返回后立刻处于 REVOKED，下一次鉴权即被拒绝。
    """
    now = time.time() if now is None else now
    for old in device.credentials.values():
        if old.state is CredentialState.ACTIVE:
            old.state = CredentialState.REVOKED
            old.revoked_at = now
    return issue_credential(device, ttl_seconds=ttl_seconds, now=now)


def revoke_credential(
    device: Device, credential_id: str, *, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    cred = device.credentials.get(credential_id)
    if cred is None:
        raise GatewayError(
            ErrorCode.CREDENTIAL_NOT_FOUND,
            f"凭据 {credential_id} 不存在",
            device_id=device.device_id,
        )
    cred.state = CredentialState.REVOKED
    cred.revoked_at = now


def authenticate(
    device: Device,
    credential_id: str | None,
    secret: str | None,
    *,
    now: float | None = None,
) -> Credential:
    """校验设备凭据，失败抛出原因码可区分的 GatewayError。

    判定顺序：缺少凭据 -> 凭据不存在 -> 已吊销 -> 已过期 -> 密钥不匹配。
    """
    now = time.time() if now is None else now
    if not credential_id or not secret:
        raise GatewayError(
            ErrorCode.CREDENTIAL_MISSING,
            "请求未携带接入凭据",
            device_id=device.device_id,
        )
    cred = device.credentials.get(credential_id)
    if cred is None:
        raise GatewayError(
            ErrorCode.CREDENTIAL_NOT_FOUND,
            f"凭据 {credential_id} 不存在（可能已轮换）",
            device_id=device.device_id,
        )
    if cred.state is CredentialState.REVOKED:
        raise GatewayError(
            ErrorCode.CREDENTIAL_REVOKED,
            f"凭据 {credential_id} 已被吊销",
            device_id=device.device_id,
        )
    if cred.is_expired(now):
        raise GatewayError(
            ErrorCode.CREDENTIAL_EXPIRED,
            f"凭据 {credential_id} 已于 {cred.expires_at} 过期",
            device_id=device.device_id,
        )
    if not _secrets.compare_digest(cred.secret, secret):
        # 密钥不正确：凭据身份组合无效，按"凭据不存在"拒绝
        raise GatewayError(
            ErrorCode.CREDENTIAL_NOT_FOUND,
            "凭据密钥不匹配",
            device_id=device.device_id,
        )
    return cred
