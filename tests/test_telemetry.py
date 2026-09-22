"""遥测：重复/乱序/时间戳回跳幂等，非法/配额拒绝原因可区分。"""
from __future__ import annotations

import logging

import pytest

from app.errors import TelemetryError
from app.models import TelemetryReason
from app.service import Gateway


def _reg(gw, device_id="dev-1"):
    gw.auth.register_device(device_id)
    return gw.auth.issue_credential(device_id)["token"]


def _report(seq, ts, **metrics):
    return {"seq": seq, "ts": ts, "metrics": metrics or {"temp": 1.0}}


def test_accept_and_idempotent_duplicate(gateway, caplog):
    _reg(gateway)
    caplog.set_level(logging.INFO, logger="iot.telemetry")

    r1 = gateway.telemetry.ingest("dev-1", _report(1, 100.0, temp=36.1))
    assert r1["reason"] == TelemetryReason.ACCEPTED.value
    r2 = gateway.telemetry.ingest("dev-1", _report(1, 100.0, temp=36.1))
    # 重复上报幂等：不重复计数，且原因可区分
    assert r2["reason"] == TelemetryReason.DUPLICATE_REPLAY.value
    stats = gateway.telemetry.stats("dev-1")
    assert stats["accepted_count"] == 1
    assert stats["duplicate_count"] == 1

    # 日志打印设备标识、上报序号与判定依据
    records = [r.getMessage() for r in caplog.records]
    assert any("device_id=dev-1" in m and "seq=1" in m
               and "basis=accepted" in m for m in records)
    assert any("basis=duplicate_replay" in m for m in records)


def test_out_of_order_and_timestamp_regression(gateway):
    _reg(gateway)
    gateway.telemetry.ingest("dev-1", _report(3, 300.0))
    gateway.telemetry.ingest("dev-1", _report(1, 100.0))   # 乱序
    gateway.telemetry.ingest("dev-1", _report(2, 200.0))
    gateway.telemetry.ingest("dev-1", _report(4, 150.0))   # 时间戳回跳
    # seq=1 再来一次（迟到重放）仍是幂等
    replay = gateway.telemetry.ingest("dev-1", _report(1, 999.0))

    assert replay["reason"] == TelemetryReason.DUPLICATE_REPLAY.value
    stats = gateway.telemetry.stats("dev-1")
    assert stats["accepted_count"] == 4       # 不丢失、不重复
    assert stats["duplicate_count"] == 1
    assert stats["max_seq"] == 4
    assert stats["max_ts"] == 300.0           # 回跳不覆盖最大时间戳
    assert sorted(stats["seen_seqs"]) == [1, 2, 3, 4]


def test_invalid_payload_rejected(gateway):
    _reg(gateway)
    for bad in [
        "not-a-dict",
        {"seq": -1, "ts": 1.0, "metrics": {"a": 1}},          # seq 非法
        {"seq": 1, "ts": 0, "metrics": {"a": 1}},             # ts 非法
        {"seq": 1, "ts": 1.0, "metrics": {}},                 # metrics 空
        {"seq": 1, "ts": 1.0, "metrics": {"a": "nan"}},       # 值非数值
    ]:
        with pytest.raises(TelemetryError) as exc:
            gateway.telemetry.ingest("dev-1", bad)
        assert exc.value.reason == TelemetryReason.INVALID_PAYLOAD.value
        assert exc.value.http_status == 422
    # 非法上报不计入任何统计
    stats = gateway.telemetry.stats("dev-1")
    assert stats["accepted_count"] == 0
    assert stats["rejected_count"] == 0


def test_quota_exceeded_rejected(tmp_path):
    gw = Gateway(tmp_path / "s.json",
                 quota_window_seconds=60, quota_max_per_window=2)
    _reg(gw)
    assert gw.telemetry.ingest("dev-1", _report(1, 1.0))["reason"] == "accepted"
    assert gw.telemetry.ingest("dev-1", _report(2, 2.0))["reason"] == "accepted"
    with pytest.raises(TelemetryError) as exc:
        gw.telemetry.ingest("dev-1", _report(3, 3.0))
    assert exc.value.reason == TelemetryReason.QUOTA_EXCEEDED.value
    assert exc.value.http_status == 429

    # 配额满后，旧 seq 重放仍判定为幂等重放（与超限拒绝可区分）
    replay = gw.telemetry.ingest("dev-1", _report(1, 1.0))
    assert replay["reason"] == TelemetryReason.DUPLICATE_REPLAY.value

    stats = gw.telemetry.stats("dev-1")
    assert stats["accepted_count"] == 2
    assert stats["rejected_count"] == 1
    assert stats["duplicate_count"] == 1


def test_next_window_quota_resets(tmp_path):
    now = [100.0]
    gw = Gateway(tmp_path / "s.json",
                 quota_window_seconds=60, quota_max_per_window=1,
                 clock=lambda: now[0])
    _reg(gw)
    gw.telemetry.ingest("dev-1", _report(1, 1.0))
    now[0] += 61
    r = gw.telemetry.ingest("dev-1", _report(2, 2.0))
    assert r["reason"] == TelemetryReason.ACCEPTED.value
