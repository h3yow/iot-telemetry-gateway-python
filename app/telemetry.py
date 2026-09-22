"""遥测接收与幂等处理。

判定顺序与语义：
1. 格式校验失败 -> INVALID_PAYLOAD（拒绝，422），不写入任何统计；
2. seq 已见过    -> DUPLICATE_REPLAY（幂等重放，200，不重复计数）；
   即使此时配额已满，重放仍返回 duplicate_replay，与「超限拒绝」可区分；
3. 超出固定窗口配额 -> QUOTA_EXCEEDED（拒绝，429）；
4. 其余 -> ACCEPTED。

乱序（seq 比 max_seq 小但从未见过）与时间戳回跳（ts 比 max_ts 小）
均正常受理：去重以 (device, seq) 为准，max_ts 取历史最大值，
因此不会重复计数，也不会因回跳丢数据。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable

from app.errors import TelemetryError
from app.models import TelemetryReason, TelemetryRecord
from app.store import Store

logger = logging.getLogger("iot.telemetry")


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_report(report: object) -> tuple[bool, str]:
    if not isinstance(report, dict):
        return False, "上报体必须是 JSON 对象"
    seq = report.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return False, "seq 必须是非负整数"
    ts = report.get("ts")
    if not _is_number(ts) or ts <= 0:
        return False, "ts 必须是正数时间戳"
    metrics = report.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return False, "metrics 必须是非空对象"
    for key, value in metrics.items():
        if not isinstance(key, str) or not _is_number(value):
            return False, "metrics 的值必须是有限数值"
    return True, ""


class TelemetryService:
    def __init__(self, store: Store, clock: Callable[[], float] | None = None,
                 quota_window_seconds: float = 60.0,
                 quota_max_per_window: int = 100):
        self.store = store
        self.clock = clock or time.time
        self.quota_window_seconds = quota_window_seconds
        self.quota_max_per_window = quota_max_per_window

    def ingest(self, device_id: str, report: object) -> dict:
        valid, why = validate_report(report)
        seq = report.get("seq") if isinstance(report, dict) else None
        if not valid:
            logger.info(
                "telemetry decision device_id=%s seq=%s basis=%s detail=%s",
                device_id, seq, TelemetryReason.INVALID_PAYLOAD.value, why)
            raise TelemetryError(TelemetryReason.INVALID_PAYLOAD.value, why)

        with self.store.lock():
            stats = self.store.get_stats(device_id)
            seq = report["seq"]
            ts = float(report["ts"])
            now = self.clock()

            # 幂等：同设备同 seq 即重放，绝不重复计数
            if seq in stats.seen_seqs:
                stats.duplicate_count += 1
                self.store.put_stats(stats)
                self.store.save()
                logger.info(
                    "telemetry decision device_id=%s seq=%s basis=%s "
                    "accepted_count=%s duplicate_count=%s",
                    device_id, seq, TelemetryReason.DUPLICATE_REPLAY.value,
                    stats.accepted_count, stats.duplicate_count)
                return {"device_id": device_id, "seq": seq,
                        "reason": TelemetryReason.DUPLICATE_REPLAY.value,
                        "stats": stats.to_dict()}

            # 配额（固定窗口）
            window = str(int(now // self.quota_window_seconds))
            used = stats.quota_used_windows.get(window, 0)
            if used >= self.quota_max_per_window:
                stats.rejected_count += 1
                self.store.put_stats(stats)
                self.store.save()
                logger.info(
                    "telemetry decision device_id=%s seq=%s basis=%s "
                    "window=%s used=%s limit=%s",
                    device_id, seq, TelemetryReason.QUOTA_EXCEEDED.value,
                    window, used, self.quota_max_per_window)
                raise TelemetryError(
                    TelemetryReason.QUOTA_EXCEEDED.value,
                    f"窗口 {window} 配额已用尽（{used}/"
                    f"{self.quota_max_per_window}）",
                    http_status=429)

            # 受理：乱序/回跳不影响计数
            basis_parts = [TelemetryReason.ACCEPTED.value]
            if stats.max_seq is not None and seq < stats.max_seq:
                basis_parts.append("out_of_order")
            if stats.max_ts is not None and ts < stats.max_ts:
                basis_parts.append("ts_regression")
            stats.seen_seqs.add(seq)
            stats.records.append(TelemetryRecord(
                seq=seq, ts=ts, received_at=now,
                payload=dict(report["metrics"])))
            stats.accepted_count += 1
            stats.max_seq = seq if stats.max_seq is None else max(stats.max_seq, seq)
            stats.max_ts = ts if stats.max_ts is None else max(stats.max_ts, ts)
            stats.quota_used_windows[window] = used + 1
            self.store.put_stats(stats)
            self.store.save()
            logger.info(
                "telemetry decision device_id=%s seq=%s basis=%s "
                "accepted_count=%s max_seq=%s max_ts=%s",
                device_id, seq, "+".join(basis_parts),
                stats.accepted_count, stats.max_seq, stats.max_ts)
            return {"device_id": device_id, "seq": seq,
                    "reason": TelemetryReason.ACCEPTED.value,
                    "stats": stats.to_dict()}

    def stats(self, device_id: str) -> dict:
        with self.store.lock():
            return self.store.get_stats(device_id).to_dict()
