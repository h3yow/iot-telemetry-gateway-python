"""设备注册与接入鉴权：轮换/吊销/过期/未注册的拒绝原因必须可区分。"""

from __future__ import annotations

import logging
import unittest

from gateway.credentials import (
    authenticate, issue_credential, revoke_credential, rotate_credentials,
)
from gateway.devices import DeviceRegistry
from gateway.errors import ErrorCode, GatewayError
from gateway.models import Device

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("cred-test")


def _expect(code: str, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except GatewayError as exc:
        assert exc.code == code, f"期望 {code}，实际 {exc.code}"
        return exc
    raise AssertionError(f"期望抛出 {code}，但调用成功")


class CredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = DeviceRegistry()
        self.dev: Device = self.reg.register("dev-1")
        self.cred = issue_credential(self.dev, ttl_seconds=100, now=1000.0)

    def test_valid_credential_passes(self) -> None:
        got = authenticate(self.dev, self.cred.credential_id,
                           self.cred.secret, now=1050.0)
        log.info("device=%s seq=- 判定依据=有效凭据通过 cred=%s",
                 self.dev.device_id, got.credential_id)
        self.assertEqual(got.credential_id, self.cred.credential_id)

    def test_unregistered_device_rejected(self) -> None:
        exc = _expect(ErrorCode.DEVICE_NOT_FOUND, self.reg.get, "ghost")
        log.info("device=ghost seq=- 判定依据=%s", exc.code)

    def test_missing_credential_rejected(self) -> None:
        exc = _expect(ErrorCode.CREDENTIAL_MISSING, authenticate,
                      self.dev, None, None, now=1001.0)
        log.info("device=%s seq=- 判定依据=%s", self.dev.device_id, exc.code)

    def test_unknown_credential_rejected(self) -> None:
        exc = _expect(ErrorCode.CREDENTIAL_NOT_FOUND, authenticate,
                      self.dev, "cred-nope", "x", now=1001.0)
        log.info("device=%s seq=- 判定依据=%s", self.dev.device_id, exc.code)

    def test_wrong_secret_rejected_as_not_found(self) -> None:
        exc = _expect(ErrorCode.CREDENTIAL_NOT_FOUND, authenticate,
                      self.dev, self.cred.credential_id, "wrong", now=1001.0)
        log.info("device=%s seq=- 判定依据=%s(密钥不匹配)",
                 self.dev.device_id, exc.code)

    def test_expired_credential_rejected(self) -> None:
        exc = _expect(ErrorCode.CREDENTIAL_EXPIRED, authenticate,
                      self.dev, self.cred.credential_id, self.cred.secret,
                      now=1100.0)
        log.info("device=%s seq=- 判定依据=%s expires_at=%s",
                 self.dev.device_id, exc.code, self.cred.expires_at)

    def test_revocation_takes_effect_immediately(self) -> None:
        revoke_credential(self.dev, self.cred.credential_id, now=1002.0)
        exc = _expect(ErrorCode.CREDENTIAL_REVOKED, authenticate,
                      self.dev, self.cred.credential_id, self.cred.secret,
                      now=1003.0)
        log.info("device=%s seq=- 判定依据=%s 吊销后旧凭据立即失效",
                 self.dev.device_id, exc.code)

    def test_rotation_invalidates_old_credential(self) -> None:
        old_id = self.cred.credential_id
        new = rotate_credentials(self.dev, ttl_seconds=100, now=2000.0)
        log.info("device=%s seq=- 判定依据=轮换 old=%s new=%s",
                 self.dev.device_id, old_id, new.credential_id)
        # 旧凭据立即失效
        exc = _expect(ErrorCode.CREDENTIAL_REVOKED, authenticate,
                      self.dev, old_id, self.cred.secret, now=2001.0)
        self.assertEqual(exc.code, ErrorCode.CREDENTIAL_REVOKED)
        # 新凭据可用
        got = authenticate(self.dev, new.credential_id, new.secret, now=2001.0)
        self.assertEqual(got.credential_id, new.credential_id)

    def test_reject_reasons_are_distinct(self) -> None:
        reasons = {
            ErrorCode.CREDENTIAL_MISSING, ErrorCode.CREDENTIAL_NOT_FOUND,
            ErrorCode.CREDENTIAL_REVOKED, ErrorCode.CREDENTIAL_EXPIRED,
            ErrorCode.DEVICE_NOT_FOUND,
        }
        self.assertEqual(len(reasons), 5)


if __name__ == "__main__":
    unittest.main()
