"""固件分批灰度：批次成功提交版本；失败整批回滚且不影响其他批次；
并发升级互斥；状态可持久化恢复。"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import unittest

from gateway.errors import ErrorCode, GatewayError
from gateway.models import BatchState, DeviceUpgradeState
from gateway.service import GatewayService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("upg-test")


class UpgradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = GatewayService(None, telemetry_quota=100)
        for did in ("d1", "d2", "d3", "d4"):
            self.svc.register_device(did)
        self.svc.add_firmware("fw", "v9", 1024, "sha256:abc")

    def _rollout(self, members, size=2):
        r = self.svc.create_rollout("fw", members, size)
        return r["rollout_id"], r["batches"]

    def test_successful_batch_commits_versions(self) -> None:
        _, (b1,) = self._rollout(["d1"], 2)
        self.svc.start_batch(b1)
        self.svc.report_device(b1, "d1", True, "ok")
        b = self.svc.health_check(b1, "d1", True)
        self.assertEqual(b["state"], BatchState.SUCCEEDED.value)
        self.assertEqual(self.svc.device_view("d1")["current_version"], "v9")
        self.assertIsNone(self.svc.device_view("d1")["active_batch_id"])
        log.info("device=d1 seq=- 判定依据=批次成功提交版本 v9")

    def test_device_failure_rolls_back_whole_batch(self) -> None:
        _, (b1,) = self._rollout(["d1", "d2"], 2)
        self.svc.start_batch(b1)
        self.svc.report_device(b1, "d1", True, "ok")
        b = self.svc.report_device(b1, "d2", False, "刷机异常")
        self.assertEqual(b["state"], BatchState.ROLLED_BACK.value)
        self.assertEqual(b["failure_reason"], "刷机异常")
        for did in ("d1", "d2"):
            self.assertEqual(self.svc.device_view(did)["current_version"], "v0")
            self.assertIsNone(self.svc.device_view(did)["active_batch_id"])
            self.assertEqual(
                b["upgrades"][did]["state"],
                DeviceUpgradeState.ROLLED_BACK.value,
            )
        log.info("device=d1,d2 seq=- 判定依据=批次%s 整批回滚，版本均保持 v0",
                 b1)

    def test_health_failure_rollback_isolates_other_batches(self) -> None:
        _, batches = self._rollout(["d1", "d2", "d3", "d4"], 2)
        b1, b2 = batches
        # 批次1成功
        self.svc.start_batch(b1)
        for did in ("d1", "d2"):
            self.svc.report_device(b1, did, True)
            self.svc.health_check(b1, did, True)
        # 批次2健康检查失败
        self.svc.start_batch(b2)
        self.svc.report_device(b2, "d3", True)
        self.svc.report_device(b2, "d4", True)
        failed = self.svc.health_check(b2, "d3", False)
        self.assertEqual(failed["state"], BatchState.ROLLED_BACK.value)
        # 批次1完全不受影响
        self.assertEqual(self.svc.get_batch(b1)["state"],
                         BatchState.SUCCEEDED.value)
        self.assertEqual(self.svc.device_view("d1")["current_version"], "v9")
        self.assertEqual(self.svc.device_view("d2")["current_version"], "v9")
        # 批次2版本回滚
        self.assertEqual(self.svc.device_view("d3")["current_version"], "v0")
        self.assertEqual(self.svc.device_view("d4")["current_version"], "v0")
        log.info("device=d3 seq=- 判定依据=%s；其他批次 d1/d2 仍为 v9",
                 failed["failure_reason"])

    def test_concurrent_rollout_rejected_with_device_busy(self) -> None:
        # 第一次升级占用 d1（PENDING 即占用）
        self._rollout(["d1", "d2"], 2)
        try:
            self.svc.create_rollout("fw", ["d1"], 1)
        except GatewayError as exc:
            self.assertEqual(exc.code, ErrorCode.DEVICE_BUSY)
            log.info("device=d1 seq=- 判定依据=%s 并发升级被拒绝", exc.code)
        else:
            self.fail("并发占用同一设备应被拒绝")

    def test_concurrent_rollout_threads_do_not_overwrite(self) -> None:
        """两个线程并发创建含相同设备的升级任务，恰一个成功，另一个 DEVICE_BUSY。"""
        errors: list[str] = []

        def worker():
            try:
                self.svc.create_rollout("fw", ["d1", "d2"], 2)
            except GatewayError as exc:
                errors.append(exc.code)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0], ErrorCode.DEVICE_BUSY)
        log.info("device=d1 seq=- 判定依据=并发竞争后仅一个批次占用，错误=%s",
                 errors[0])

    def test_duplicate_report_is_idempotent(self) -> None:
        _, (b1,) = self._rollout(["d1"], 2)
        self.svc.start_batch(b1)
        self.svc.report_device(b1, "d1", True)
        self.svc.report_device(b1, "d1", True)  # 重复上报不改变进度
        b = self.svc.get_batch(b1)
        self.assertEqual(b["upgrades"]["d1"]["state"],
                         DeviceUpgradeState.REBOOTING.value)
        log.info("device=d1 seq=- 判定依据=重复 report 幂等，状态保持 REBOOTING")

    def test_restart_preserves_rollout_state_and_versions(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            svc = GatewayService(path)
            svc.register_device("x1")
            svc.add_firmware("fw2", "v7", 10, "sum")
            r = svc.create_rollout("fw2", ["x1"], 1)
            bid = r["batches"][0]
            svc.start_batch(bid)
            svc.report_device(bid, "x1", False, "模拟失败")
            self.assertEqual(svc.device_view("x1")["current_version"], "v0")

            # 重启：新实例加载同一状态文件
            svc2 = GatewayService(path)
            b = svc2.get_batch(bid)
            self.assertEqual(b["state"], BatchState.ROLLED_BACK.value)
            self.assertEqual(svc2.device_view("x1")["current_version"], "v0")
            self.assertIsNone(svc2.device_view("x1")["active_batch_id"])
            log.info("device=x1 seq=- 判定依据=重启后批次状态=%s 版本=%s 一致",
                     b["state"], svc2.device_view("x1")["current_version"])
        finally:
            os.unlink(path)

    def test_rollback_terminal_batch_rejected(self) -> None:
        _, (b1,) = self._rollout(["d1"], 2)
        self.svc.start_batch(b1)
        self.svc.report_device(b1, "d1", True)
        self.svc.health_check(b1, "d1", True)
        try:
            self.svc.rollback_batch(b1)
        except GatewayError as exc:
            self.assertEqual(exc.code, ErrorCode.BATCH_NOT_ACTIVE)
            log.info("device=d1 seq=- 判定依据=%s 终态批次不可回滚", exc.code)
        else:
            self.fail("已成功批次不应允许回滚")


if __name__ == "__main__":
    unittest.main()
