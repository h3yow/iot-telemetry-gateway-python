"""遥测幂等处理。

语义：
- 设备侧 ``seq`` 单调递增，作为去重键；乱序到达不影响正确性。
- 已受理过的 seq 再次上报一律视为幂等重放（DUPLICATE），
  即使此时配额已满也仍然按重放处理，从而与"配额拒绝"可区分。
- 时间戳回跳（ts 变小）不更新 ``last_ts``，指标只按 seq 累加一次，
  因此重复/乱序/回跳都不会重复计数或丢统计。
"""

from __future__ import annotations

import numbers
from typing import Any

from .errors import ErrorCode, GatewayError
from .models import TelemetryReport, TelemetryStats

# 判定原因（稳定字符串，HTTP/CLI/测试统一引用）
ACCEPTED = "ACCEPTED"
DUPLICATE = "DUPLICATE"
REJECTED_FORMAT = "REJECTED_FORMAT"
REJECTED_QUOTA = "REJECTED_QUOTA"


def validate_payload(device_id: str, raw: Any) -> TelemetryReport:
    """把原始上报体校验并转换为 TelemetryReport；非法抛 TELEMETRY_BAD_FORMAT。"""
    if not isinstance(raw, dict):
        raise GatewayError(
            ErrorCode.TELEMETRY_BAD_FORMAT, "上报体必须是 JSON 对象",
            device_id=device_id,
        )
    if "seq" not in raw:
        raise GatewayError(
            ErrorCode.TELEMETRY_BAD_FORMAT, "缺少必填字段 seq", device_id=device_id
        )
    seq = raw["seq"]
    # bool 是 int 的子类，显式排除
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise GatewayError(
            ErrorCode.TELEMETRY_BAD_FORMAT,
            "seq 必须是非负整数", device_id=device_id,
        )
    if "ts" not in raw or not isinstance(raw["ts"], numbers.Real) or isinstance(
        raw["ts"], bool
    ):
        raise GatewayError(
            ErrorCode.TELEMETRY_BAD_FORMAT, "ts 必须是数值型时间戳",
            device_id=device_id,
        )
    metrics = raw.get("metrics", {})
    if not isinstance(metrics, dict) or not metrics:
        raise GatewayError(
            ErrorCode.TELEMETRY_BAD_FORMAT,
            "metrics 必须是非空对象", device_id=device_id,
        )
    clean: dict[str, float] = {}
    for key, value in metrics.items():
        if not isinstance(key, str) or not key:
            raise GatewayError(
                ErrorCode.TELEMETRY_BAD_FORMAT,
                "metrics 的键必须是非空字符串", device_id=device_id,
            )
        if not isinstance(value, numbers.Real) or isinstance(value, bool):
            raise GatewayError(
                ErrorCode.TELEMETRY_BAD_FORMAT,
                f"metrics.{key} 必须是数值", device_id=device_id,
            )
        clean[key] = float(value)
    return TelemetryReport(device_id=device_id, seq=seq, ts=float(raw["ts"]),
                           metrics=clean)


def process_report(
    stats: TelemetryStats,
    report: TelemetryReport,
    *,
    quota: int,
    now: float = 0.0,
) -> dict[str, Any]:
    """处理一条已通过格式校验的上报，返回可解释的判定结论。

    返回示例::

        {"accepted": True, "duplicate": False, "reason": "ACCEPTED", ...}
    """
    # 1) 幂等重放优先：见过的 seq 永远原样返回，不计费/不累加/不报配额
    if report.seq in stats.accepted_seq:
        stats.duplicates += 1
        return {
            "accepted": True,
            "duplicate": True,
            "reason": DUPLICATE,
            "device_id": report.device_id,
            "seq": report.seq,
            "detail": f"seq={report.seq} 已受理过，按幂等重放处理，统计不变",
        }

    # 2) 配额：仅限"新 seq"。配额满后重放仍可成功，二者可区分
    if stats.received >= quota:
        stats.rejected += 1
        return {
            "accepted": False,
            "duplicate": False,
            "reason": REJECTED_QUOTA,
            "device_id": report.device_id,
            "seq": report.seq,
            "detail": f"设备配额 {quota} 条已满，拒绝新 seq={report.seq}",
        }

    # 3) 首次受理：计数与累加各一次；乱序/回跳安全
    stats.received += 1
    stats.accepted_seq[report.seq] = True
    if stats.last_seq is None or report.seq > stats.last_seq:
        stats.last_seq = report.seq
    old_last_ts = stats.last_ts
    backtrack = old_last_ts is not None and report.ts < old_last_ts
    if old_last_ts is None or report.ts >= old_last_ts:
        # 相等或更新才推进；回跳（更小）不更新
        stats.last_ts = report.ts
    for key, value in report.metrics.items():
        stats.totals[key] = stats.totals.get(key, 0.0) + value

    return {
        "accepted": True,
        "duplicate": False,
        "reason": ACCEPTED,
        "device_id": report.device_id,
        "seq": report.seq,
        "detail": (
            f"seq={report.seq} 首次受理并计入统计"
            + ("（设备时间戳回跳，last_ts 不更新）" if backtrack else "")
        ),
    }
