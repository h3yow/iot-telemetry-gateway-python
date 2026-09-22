"""HTTP 入口测试：断言错误码/状态码与 service 层结论一致（各入口一致性）。"""

from __future__ import annotations

import logging
import unittest

from fastapi.testclient import TestClient

from gateway.api import create_app
from gateway.errors import ErrorCode
from gateway.service import GatewayService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("http-test")


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = GatewayService(None, telemetry_quota=3)
        self.client = TestClient(create_app(self.svc))

    def _register(self, did="dev-h"):
        r = self.client.post("/devices", json={"device_id": did})
        self.assertEqual(r.status_code, 201)
        return r.json()

    def test_auth_failures_have_distinct_codes(self) -> None:
        cred = self._register()
        # 无凭据
        r = self.client.post(f"/devices/{cred['device_id']}/telemetry",
                             json={"seq": 1, "ts": 1.0, "metrics": {"t": 1}})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"], ErrorCode.CREDENTIAL_MISSING)
        # 未注册设备
        r = self.client.post("/devices/nope/telemetry",
                             headers={"X-Credential-Id": "a", "X-Credential-Secret": "b"},
                             json={"seq": 1, "ts": 1.0, "metrics": {"t": 1}})
        self.assertEqual(r.json()["error"], ErrorCode.DEVICE_NOT_FOUND)
        # 吊销后立即失效
        self.client.post(
            f"/devices/{cred['device_id']}/credentials/"
            f"{cred['credential_id']}/revoke")
        r = self.client.post(
            f"/devices/{cred['device_id']}/telemetry",
            headers={"X-Credential-Id": cred["credential_id"],
                     "X-Credential-Secret": cred["secret"]},
            json={"seq": 1, "ts": 1.0, "metrics": {"t": 1}})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"], ErrorCode.CREDENTIAL_REVOKED)
        log.info("device=%s seq=- 判定依据=HTTP 拒绝码分别为 "
                 "MISSING/DEVICE_NOT_FOUND/REVOKED", cred["device_id"])

    def test_telemetry_idempotency_over_http(self) -> None:
        cred = self._register()
        h = {"X-Credential-Id": cred["credential_id"],
             "X-Credential-Secret": cred["secret"]}
        did = cred["device_id"]

        def post(seq, ts):
            return self.client.post(
                f"/devices/{did}/telemetry", headers=h,
                json={"seq": seq, "ts": ts, "metrics": {"temp": 1.0}})

        self.assertEqual(post(2, 20.0).json()["reason"], "ACCEPTED")  # 乱序
        self.assertEqual(post(1, 10.0).json()["reason"], "ACCEPTED")
        self.assertEqual(post(2, 20.0).json()["reason"], "DUPLICATE")
        bad = post("x", 1.0)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"], ErrorCode.TELEMETRY_BAD_FORMAT)
        post(3, 30.0)  # 第3条，配额满
        q = post(4, 40.0)
        self.assertEqual(q.status_code, 429)
        self.assertEqual(q.json()["error"], ErrorCode.TELEMETRY_QUOTA_EXCEEDED)
        replay = post(1, 10.0)
        self.assertEqual(replay.json()["reason"], "DUPLICATE")
        stats = self.client.get(f"/devices/{did}/stats").json()
        self.assertEqual(stats["received"], 3)
        self.assertEqual(stats["duplicates"], 2)
        self.assertEqual(stats["totals"]["temp"], 3.0)
        log.info("device=%s seq=1..4 判定依据=乱序接受/重复幂等/非法400/配额429, "
                 "received=%s", did, stats["received"])

    def test_batch_failure_and_rollback_over_http(self) -> None:
        c1, c2 = self._register("p1"), self._register("p2")
        self.client.post("/firmware", json={
            "firmware_id": "fw", "version": "v3", "size": 1, "checksum": "s"})
        r = self.client.post("/rollouts", json={
            "firmware_id": "fw", "device_ids": ["p1", "p2"], "batch_size": 2})
        bid = r.json()["batches"][0]
        self.client.post(f"/batches/{bid}/start")
        self.client.post(f"/batches/{bid}/report",
                         json={"device_id": "p1", "success": True})
        failed = self.client.post(f"/batches/{bid}/report",
                                  json={"device_id": "p2", "success": False,
                                        "message": "设备异常"})
        self.assertEqual(failed.json()["state"], "ROLLED_BACK")
        for did in ("p1", "p2"):
            ver = self.client.get(f"/devices/{did}").json()["current_version"]
            self.assertEqual(ver, "v0")
        # 并发再建批次 -> 回滚后设备已释放，可以成功；回滚中占用期间拒绝
        busy = self.client.post("/rollouts", json={
            "firmware_id": "fw", "device_ids": ["p1"], "batch_size": 1})
        self.assertEqual(busy.status_code, 201)
        log.info("device=p1,p2 seq=- 判定依据=HTTP 批次失败整体回滚，版本保持 v0")

    def test_concurrent_rollout_conflict_returns_409(self) -> None:
        self._register("z1")
        self.client.post("/firmware", json={
            "firmware_id": "fw", "version": "v3", "size": 1, "checksum": "s"})
        self.client.post("/rollouts", json={
            "firmware_id": "fw", "device_ids": ["z1"], "batch_size": 1})
        r = self.client.post("/rollouts", json={
            "firmware_id": "fw", "device_ids": ["z1"], "batch_size": 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], ErrorCode.DEVICE_BUSY)
        log.info("device=z1 seq=- 判定依据=HTTP 409 %s", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
