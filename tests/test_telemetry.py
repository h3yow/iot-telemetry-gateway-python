"""遥测幂等：重复/乱序/时间戳回跳不重复计数、不丢统计；
非法格式与配额拒绝和幂等重放结论可区分。"""

from __future__ import annotations

import logging
import unittest

from gateway.errors import ErrorCode, GatewayError
from gateway.models import TelemetryReport, TelemetryStats
from gateway.telemetry import (
    ACCEPTED, DUPLICATE, REJECTED_FORMAT, REJECTED_QUOTA,
    process_report, validate_payload,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tel-test")

DID = "dev-tel"


def _feed(stats: TelemetryStats, seq: int, ts: float,
          metrics=None, quota: int = 1000) -> dict:
    metrics = metrics or {"temp": 1.0}
    r = process_report(
        stats, TelemetryReport(DID, seq, ts, dict(metrics)),
        quota=quota, now=ts,
    )
    log.info("device=%s seq=%s ts=%s 判定=%s 依据=%s",
             DID, seq, ts, r["reason"], r["detail"])
    return r


class TelemetryIdempotencyTest(unittest.TestCase):
    def test_out_of_order_counts_each_seq_once(self) -> None:
        stats = TelemetryStats()
        for seq, ts in [(3, 30.0), (1, 10.0), (2, 20.0)]:  # 乱序
            r = _feed(stats, seq, ts)
            self.assertEqual(r["reason"], ACCEPTED)
        self.assertEqual(stats.received, 3)
        self.assertEqual(stats.last_seq, 3)
        self.assertEqual(stats.last_ts, 30.0)
        self.assertEqual(stats.totals["temp"], 3.0)

    def test_duplicate_is_idempotent_and_distinct(self) -> None:
        stats = TelemetryStats()
        _feed(stats, 1, 10.0, {"temp": 5.0})
        r2 = _feed(stats, 1, 10.0, {"temp": 5.0})  # 完全重复
        r3 = _feed(stats, 1, 99.0, {"temp": 9.0})  # 重复 seq 但内容不同
        self.assertEqual(r2["reason"], DUPLICATE)
        self.assertEqual(r3["reason"], DUPLICATE)
        self.assertEqual(stats.received, 1)          # 只计一次
        self.assertEqual(stats.duplicates, 2)
        self.assertEqual(stats.totals["temp"], 5.0)  # 不重复累加
        self.assertEqual(stats.last_ts, 10.0)        # 重放不推进时间

    def test_timestamp_backtrack_keeps_last_ts(self) -> None:
        stats = TelemetryStats()
        _feed(stats, 1, 100.0, {"temp": 1.0})
        _feed(stats, 2, 80.0, {"temp": 2.0})   # 新 seq，但 ts 回跳
        self.assertEqual(stats.last_ts, 100.0)
        self.assertEqual(stats.received, 2)     # 数据不丢失
        self.assertEqual(stats.totals["temp"], 3.0)

    def test_quota_rejection_vs_duplicate_replay(self) -> None:
        stats = TelemetryStats()
        r1 = _feed(stats, 1, 10.0, quota=1)
        r2 = _feed(stats, 2, 20.0, quota=1)   # 新 seq 超配额
        r3 = _feed(stats, 1, 10.0, quota=1)   # 旧 seq 重放
        self.assertEqual(r1["reason"], ACCEPTED)
        self.assertEqual(r2["reason"], REJECTED_QUOTA)
        self.assertEqual(r3["reason"], DUPLICATE)  # 与配额拒绝可区分
        self.assertEqual(stats.received, 1)
        self.assertEqual(stats.rejected, 1)
        self.assertEqual(stats.duplicates, 1)

    def test_invalid_format_rejected_with_distinct_code(self) -> None:
        bad_payloads = [
            ("not-dict", "上报体非对象"),
            ({"ts": 1.0, "metrics": {"temp": 1.0}}, "缺 seq"),
            ({"seq": -1, "ts": 1.0, "metrics": {"temp": 1.0}}, "seq 负数"),
            ({"seq": "1", "ts": 1.0, "metrics": {"temp": 1.0}}, "seq 非整数"),
            ({"seq": 1, "metrics": {"temp": 1.0}}, "缺 ts"),
            ({"seq": 1, "ts": 1.0, "metrics": {}}, "metrics 为空"),
            ({"seq": 1, "ts": 1.0, "metrics": {"temp": "hot"}}, "指标非数值"),
        ]
        for payload, label in bad_payloads:
            with self.subTest(label=label):
                try:
                    validate_payload(DID, payload)
                except GatewayError as exc:
                    self.assertEqual(exc.code, ErrorCode.TELEMETRY_BAD_FORMAT)
                    log.info("device=%s seq=- 判定依据=%s (%s)",
                             DID, exc.code, label)
                else:
                    self.fail(f"{label} 应被拒绝")

    def test_stats_consistency_under_mixed_stream(self) -> None:
        """混合流：乱序+重复+回跳+非法+配额，统计闭合。"""
        stats = TelemetryStats()
        stream = [
            (2, 20.0, 1.0), (1, 10.0, 1.0), (2, 20.0, 1.0),
            (3, 5.0, 1.0), (4, 40.0, 1.0),
        ]
        for seq, ts, val in stream:
            _feed(stats, seq, ts, {"temp": val}, quota=3)
        # accepted seqs: 2,1,3；seq4 被配额拒绝；seq2 重放1次
        self.assertEqual(stats.received, 3)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(stats.rejected, 1)
        self.assertEqual(stats.last_seq, 3)
        self.assertEqual(stats.last_ts, 20.0)  # seq3 ts=5 回跳不更新
        self.assertEqual(stats.totals["temp"], 3.0)
        log.info("device=%s seq=- 最终统计 received=%s duplicates=%s "
                 "rejected=%s totals=%s", DID, stats.received, stats.duplicates,
                 stats.rejected, stats.totals)


if __name__ == "__main__":
    unittest.main()
