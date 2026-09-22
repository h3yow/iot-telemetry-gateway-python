"""设备注册、凭据签发/轮换/吊销与接入鉴权。

令牌格式：Bearer base64(credential_id + ":" + secret)
服务端只存 secret 的 sha256。
轮换（为同一设备签发新凭据）时，旧活动凭据立刻置为 expired，
因此「凭据轮换或吊销后旧凭据必须立即失效」。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import uuid
from typing import Callable

from app.errors import AuthError, ConflictError
from app.models import AuthReason, Credential, CredentialStatus, Device
from app.store import Store

logger = logging.getLogger("iot.auth")


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def encode_token(credential_id: str, secret: str) -> str:
    raw = f"{credential_id}:{secret}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str) -> tuple[str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        credential_id, secret = raw.split(":", 1)
        if not credential_id or not secret:
            return None
        return credential_id, secret
    except (ValueError, UnicodeDecodeError):
        return None


class AuthService:
    def __init__(self, store: Store, ttl_seconds: float = 86400.0,
                 clock: Callable[[], float] | None = None):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.clock = clock or time.time

    def register_device(self, device_id: str,
                        firmware_version: str = "1.0.0") -> Device:
        with self.store.lock():
            if self.store.get_device(device_id) is not None:
                raise ConflictError(
                    "device_already_registered",
                    f"设备 {device_id} 已注册", http_status=409)
            device = Device(device_id=device_id,
                            firmware_version=firmware_version,
                            registered_at=self.clock())
            self.store.put_device(device)
            self.store.save()
            logger.info("device registered device_id=%s firmware=%s",
                        device_id, firmware_version)
            return device

    def issue_credential(self, device_id: str) -> dict:
        """签发新凭据并轮换：旧活动凭据立即失效。"""
        with self.store.lock():
            device = self.store.get_device(device_id)
            if device is None:
                raise AuthError(
                    AuthReason.DEVICE_NOT_REGISTERED.value,
                    f"设备 {device_id} 未注册", http_status=404)
            now = self.clock()
            # 轮换：旧的活动凭据立即过期
            for cred in self.store.list_credentials():
                if (cred.device_id == device_id
                        and cred.status == CredentialStatus.ACTIVE):
                    cred.status = CredentialStatus.EXPIRED
                    self.store.put_credential(cred)
            credential_id = f"cred-{uuid.uuid4().hex[:16]}"
            secret = secrets.token_urlsafe(24)
            cred = Credential(
                credential_id=credential_id,
                device_id=device_id,
                secret_hash=hash_secret(secret),
                issued_at=now,
                expires_at=now + self.ttl_seconds,
                status=CredentialStatus.ACTIVE,
                version=(device.current_credential_id is not None) + 1,
            )
            device.current_credential_id = credential_id
            self.store.put_credential(cred)
            self.store.put_device(device)
            self.store.save()
            logger.info(
                "credential issued device_id=%s credential_id=%s rotated_old=%s",
                device_id, credential_id, cred.version > 1)
            return {
                "device_id": device_id,
                "credential_id": credential_id,
                "secret": secret,
                "token": encode_token(credential_id, secret),
                "issued_at": now,
                "expires_at": cred.expires_at,
            }

    def revoke_credential(self, credential_id: str) -> Credential:
        with self.store.lock():
            cred = self.store.get_credential(credential_id)
            if cred is None:
                raise AuthError(
                    AuthReason.CREDENTIAL_NOT_FOUND.value,
                    f"凭据 {credential_id} 不存在", http_status=404)
            if cred.status == CredentialStatus.ACTIVE:
                cred.status = CredentialStatus.REVOKED
                self.store.put_credential(cred)
                device = self.store.get_device(cred.device_id)
                if device and device.current_credential_id == credential_id:
                    device.current_credential_id = None
                    self.store.put_device(device)
                self.store.save()
            logger.info("credential revoked credential_id=%s device_id=%s",
                        credential_id, cred.device_id)
            return cred

    def authenticate(self, device_id: str, token: str
                     ) -> tuple[bool, AuthReason, Credential | None]:
        """返回 (是否通过, 原因码, 凭据)。原因码彼此可区分。"""
        with self.store.lock():
            device = self.store.get_device(device_id)
            if device is None:
                return False, AuthReason.DEVICE_NOT_REGISTERED, None

            decoded = decode_token(token or "")
            if decoded is None:
                return False, AuthReason.CREDENTIAL_NOT_FOUND, None
            credential_id, secret = decoded

            cred = self.store.get_credential(credential_id)
            # 凭据不存在或不属于该设备：对该设备而言等同凭据不存在
            if cred is None or cred.device_id != device_id:
                return False, AuthReason.CREDENTIAL_NOT_FOUND, None

            if cred.status == CredentialStatus.REVOKED:
                return False, AuthReason.CREDENTIAL_REVOKED, None

            now = self.clock()
            if (cred.status == CredentialStatus.EXPIRED
                    or cred.expires_at <= now):
                if cred.status == CredentialStatus.ACTIVE:
                    cred.status = CredentialStatus.EXPIRED
                    self.store.put_credential(cred)
                    self.store.save()
                return False, AuthReason.CREDENTIAL_EXPIRED, None

            if not secrets.compare_digest(cred.secret_hash,
                                          hash_secret(secret)):
                return False, AuthReason.SECRET_MISMATCH, None

            return True, AuthReason.OK, cred
