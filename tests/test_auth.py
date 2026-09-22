"""接入鉴权：未注册/凭据不存在/过期/吊销/密钥不符 可区分。"""
from __future__ import annotations

from app.auth import encode_token
from app.models import AuthReason, CredentialStatus


def test_register_and_authenticate_ok(gateway):
    gateway.auth.register_device("dev-1")
    issued = gateway.auth.issue_credential("dev-1")
    ok, reason, cred = gateway.auth.authenticate("dev-1", issued["token"])
    assert ok and reason == AuthReason.OK
    assert cred.credential_id == issued["credential_id"]


def test_unregistered_device_rejected(gateway):
    ok, reason, _ = gateway.auth.authenticate("ghost", "whatever")
    assert not ok
    assert reason == AuthReason.DEVICE_NOT_REGISTERED


def test_revoked_credential_rejected(gateway):
    gateway.auth.register_device("dev-1")
    issued = gateway.auth.issue_credential("dev-1")
    gateway.auth.revoke_credential(issued["credential_id"])
    # 旧凭据必须立即失效
    ok, reason, _ = gateway.auth.authenticate("dev-1", issued["token"])
    assert not ok and reason == AuthReason.CREDENTIAL_REVOKED
    assert gateway.store.get_credential(
        issued["credential_id"]).status == CredentialStatus.REVOKED


def test_expired_credential_rejected(gateway):
    now = [1000.0]
    gw = gateway
    gw.auth.clock = lambda: now[0]
    gw.auth.register_device("dev-1")
    issued = gw.auth.issue_credential("dev-1")
    now[0] += gw.auth.ttl_seconds + 1
    ok, reason, _ = gw.auth.authenticate("dev-1", issued["token"])
    assert not ok and reason == AuthReason.CREDENTIAL_EXPIRED


def test_rotation_invalidates_old_credential(gateway):
    gateway.auth.register_device("dev-1")
    first = gateway.auth.issue_credential("dev-1")
    second = gateway.auth.issue_credential("dev-1")
    # 新凭据可用
    ok, reason, _ = gateway.auth.authenticate("dev-1", second["token"])
    assert ok and reason == AuthReason.OK
    # 旧凭据轮换后立即失效，且原因是过期（与吊销区分）
    ok, reason, _ = gateway.auth.authenticate("dev-1", first["token"])
    assert not ok and reason == AuthReason.CREDENTIAL_EXPIRED


def test_wrong_secret_rejected(gateway):
    gateway.auth.register_device("dev-1")
    issued = gateway.auth.issue_credential("dev-1")
    forged = encode_token(issued["credential_id"], "wrong-secret")
    ok, reason, _ = gateway.auth.authenticate("dev-1", forged)
    assert not ok and reason == AuthReason.SECRET_MISMATCH


def test_credential_of_other_device_not_found(gateway):
    gateway.auth.register_device("dev-1")
    gateway.auth.register_device("dev-2")
    issued = gateway.auth.issue_credential("dev-1")
    ok, reason, _ = gateway.auth.authenticate("dev-2", issued["token"])
    assert not ok and reason == AuthReason.CREDENTIAL_NOT_FOUND
