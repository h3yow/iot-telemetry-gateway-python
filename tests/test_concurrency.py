"""并发场景：多线程同时上报遥测与并发升级，进度/计数/版本不得互相覆盖。"""

from __future__ import annotations

import logging
import unittest
from concurrent.futures import ThreadPoolExecutor

from gateway.errors import ErrorCode, GatewayError
from gateway.service import GatewayService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("conc-test")


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_telemetry_no_double_count_nor_loss(self) -> None:
        svc = GatewayService(None, telemetry_quota=10_000)
        cred = svc.register_device("dc")
        headers = (cred["credential_id"], cred["secret"])

        # 每个 seq 用两个线程重复提交：最终每个 seq 恰好受理一次
        seqs = list(range(1, 501))

        def send(seq: int) -> str:
            return svc.ingest_telemetry(
                "dc", headers[0], headers[1],
                {"seq": seq, "ts": float(seq), "metrics": {"temp": 1.0}},
            )["reason"]

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(send, [s for s in seqs for _ in range(2)]))

        accepted = results.count("ACCEPTED")
        duplicate = results.count("DUPLICATE")
        stats = svc.get_stats("dc")
        self.assertEqual(accepted, 500)
        self.assertEqual(duplicate, 500)
        self.assertEqual(stats["received"], 500)
        self.assertEqual(stats["duplicates"], 500)
        self.assertEqual(stats["totals"]["temp"], 500.0)
        self.assertEqual(stats["last_seq"], 500)
        log.info("device=dc seq=1..500(各重复一次) 判定依据=ACCEPTED=%s "
                 "DUPLICATE=%s received=%s 不重不漏", accepted, duplicate,
                 stats["received"])

    def test_concurrent_upgrade_only_one_wins(self) -> None:
        svc = GatewayService(None)
        svc.register_device("du")
        svc.add_firmware("fw", "v1", 1, "s")

        def create():
            try:
                svc.create_rollout("fw", ["du"], 1)
                return "OK"
            except GatewayError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: create(), range(8)))
        self.assertEqual(outcomes.count("OK"), 1)
        self.assertEqual(outcomes.count(ErrorCode.DEVICE_BUSY), 7)
        log.info("device=du seq=- 判定依据=8 并发升级仅 1 个占用设备, "
                 "DEVICE_BUSY=%s", outcomes.count(ErrorCode.DEVICE_BUSY))


if __name__ == "__main__":
    unittest.main()
