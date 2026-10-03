"""重启 / 原地升级恢复：旧快照占用重建、回滚往来版本落点、反复重启幂等。

每个用例的日志都打印业务场景与关键断言点，便于从测试日志看出
覆盖了哪些业务（pytest -o log_cli=true -o log_cli_level=INFO）。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.errors import ConflictError
from app.models import BatchState, CampaignState, DeviceUpgradeState
from app.service import Gateway

logger = logging.getLogger("test.recovery")


def _legacy_snapshot_without_occupations(path) -> None:
    """把快照改写成上一版格式：没有 device_occupations 字段。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("device_occupations", None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _setup_in_progress(gw, devices=("dev-1", "dev-2"), version="2.0.0"):
    """注册设备、发布固件、创建活动并把第一个批次推进到进行中。"""
    gw.upgrade.release_firmware(version)
    gw.upgrade.release_firmware("3.0.0")
    for d in devices:
        gw.auth.register_device(d)
    camp = gw.upgrade.create_campaign(version, list(devices),
                                      batch_size=len(devices))
    batch = camp.batches[0]
    gw.upgrade.upgrade_batch(camp.campaign_id, batch.batch_id,
                             health_results=None)
    return camp, batch


def test_legacy_snapshot_in_progress_batch_still_occupies(tmp_path):
    """原地升级：旧快照里进行中的批次，重启后设备仍被占着。"""
    path = tmp_path / "state.json"
    gw = Gateway(path)
    gw.upgrade.release_firmware("2.0.0")
    gw.upgrade.release_firmware("3.0.0")
    for d in ("dev-1", "dev-2", "dev-3"):
        gw.auth.register_device(d)
    # 两个活动共享 dev-2（与治理用例一致的搭建方式：先建活动再开批次）
    c1 = gw.upgrade.create_campaign("2.0.0", ["dev-1", "dev-2"], batch_size=2)
    c2 = gw.upgrade.create_campaign("3.0.0", ["dev-2", "dev-3"], batch_size=2)
    batch = c1.batches[0]
    gw.upgrade.upgrade_batch(c1.campaign_id, batch.batch_id,
                             health_results=None)
    _legacy_snapshot_without_occupations(path)
    logger.info("场景：沿用上一版快照（无 device_occupations）原地升级重启，"
                "活动 %s 批次 %s 仍在进行中", c1.campaign_id, batch.batch_id)

    gw2 = Gateway(path)
    # 占用查询：能查到是哪个活动、哪个批次
    occ = gw2.upgrade.device_occupation("dev-1")
    assert occ["occupied"] is True
    assert occ["occupation"]["campaign_id"] == c1.campaign_id
    assert occ["occupation"]["batch_id"] == batch.batch_id
    logger.info("占用查询恢复：dev-1 被 %s/%s 占用",
                c1.campaign_id, batch.batch_id)

    # 跨活动争抢：按独立原因码拒绝，被拒方不改动任何设备
    with pytest.raises(ConflictError) as exc:
        gw2.upgrade.upgrade_batch(c2.campaign_id, c2.batches[0].batch_id)
    assert exc.value.reason == "device_occupied_by_other_campaign"
    assert gw2.store.get_device("dev-2").firmware_version == "1.0.0"
    assert gw2.store.get_device("dev-3").firmware_version == "1.0.0"
    assert gw2.store.list_upgrade_events("dev-3") == []
    logger.info("跨活动争抢被拒：reason=%s，被拒方未改动设备",
                exc.value.reason)

    # 原批次继续推进不受影响：上报完成后走到成功终态并释放
    gw2.upgrade.report_device(c1.campaign_id, batch.batch_id,
                              "dev-1", ok=True)
    done = gw2.upgrade.report_device(c1.campaign_id, batch.batch_id,
                                     "dev-2", ok=True)
    assert done["state"] == BatchState.SUCCEEDED.value
    assert gw2.store.get_device("dev-1").firmware_version == "2.0.0"
    assert gw2.upgrade.device_occupation("dev-1")["occupied"] is False
    logger.info("原批次推进到成功终态，占用已释放")

    # 释放后设备可以被新活动收走
    r = gw2.upgrade.upgrade_batch(c2.campaign_id, c2.batches[0].batch_id,
                                  health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
    assert gw2.store.get_device("dev-2").firmware_version == "3.0.0"


def test_rollback_history_versions_match_real_landing(tmp_path):
    """回滚往来的版本落点与设备真实版本一致，重启后仍能一路追到当前版本。"""
    path = tmp_path / "state.json"
    gw = Gateway(path)
    camp, batch = _setup_in_progress(gw)
    logger.info("场景：批次健康检查失败整批回滚，核对往来记录版本落点")
    gw.upgrade.report_device(camp.campaign_id, batch.batch_id,
                             "dev-1", ok=False, message="健康检查失败")

    def assert_chain(g):
        events = g.upgrade.device_upgrade_history("dev-1")["events"]
        pairs = [(e["event_type"], e["from_version"], e["to_version"])
                 for e in events]
        assert pairs == [("upgrade_started", "1.0.0", "2.0.0"),
                         ("upgrade_rolled_back", "2.0.0", "1.0.0")]
        # 按先后顺序，每条落点衔接下一条起点，终点即设备当前版本
        for prev, nxt in zip(events, events[1:]):
            assert prev["to_version"] == nxt["from_version"]
        current = g.store.get_device("dev-1").firmware_version
        assert events[-1]["to_version"] == current == "1.0.0"

    assert_chain(gw)
    logger.info("回滚落点核对通过：2.0.0 -> 1.0.0，与设备真实版本一致")
    assert_chain(Gateway(path))  # 重启后往来记录不变
    logger.info("重启后往来记录仍一致")


def test_repeated_restarts_keep_occupation_and_release_idempotent(tmp_path):
    """反复重启：占用不丢不重；终态释放只发生一次；取消/回滚幂等。"""
    path = tmp_path / "state.json"
    gw = Gateway(path)
    camp, batch = _setup_in_progress(gw)

    # 连续重启多次：占用稳定恢复，同一台设备不会被重复占上
    for round_no in range(3):
        gw = Gateway(path)
        occ = gw.upgrade.device_occupation("dev-1")
        assert occ["occupied"] is True
        assert occ["occupation"]["campaign_id"] == camp.campaign_id
        assert len([e for e in gw.store.list_upgrade_events("dev-1")
                    if e.event_type.value == "upgrade_started"]) == 1
        logger.info("第 %d 次重启：占用仍在，started 事件未重复", round_no + 1)

    # 取消活动：进行中批次整批回滚并释放占用
    prog = gw.upgrade.cancel_campaign(camp.campaign_id)
    assert prog["state"] == CampaignState.CANCELLED.value
    assert gw.upgrade.device_occupation("dev-1")["occupied"] is False
    assert gw.store.get_device("dev-1").firmware_version == "1.0.0"
    events_after_cancel = [e.to_dict()
                           for e in gw.store.list_upgrade_events("dev-1")]

    # 重启后再重复取消：幂等，不重复回滚、不追加记录、不改版本
    gw = Gateway(path)
    gw.upgrade.cancel_campaign(camp.campaign_id)
    gw = Gateway(path)
    gw.upgrade.cancel_campaign(camp.campaign_id)
    assert [e.to_dict() for e in gw.store.list_upgrade_events("dev-1")] \
        == events_after_cancel
    assert gw.store.get_device("dev-1").firmware_version == "1.0.0"
    assert gw.upgrade.device_occupation("dev-1")["occupied"] is False
    logger.info("取消 + 多次重启 + 重复取消：记录与版本均未变化")

    # 释放后设备可正常参加后面的活动
    c2 = gw.upgrade.create_campaign("3.0.0", ["dev-1"], batch_size=1)
    r = gw.upgrade.upgrade_batch(c2.campaign_id, c2.batches[0].batch_id,
                                 health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
    assert gw.store.get_device("dev-1").firmware_version == "3.0.0"


def test_stale_occupation_of_terminal_batch_is_dropped(tmp_path):
    """快照里残留指向已终态批次的占用：重启后必须清掉，不能卡住不释放。"""
    path = tmp_path / "state.json"
    gw = Gateway(path)
    camp, batch = _setup_in_progress(gw)
    gw.upgrade.report_device(camp.campaign_id, batch.batch_id,
                             "dev-1", ok=True)
    gw.upgrade.report_device(camp.campaign_id, batch.batch_id,
                             "dev-2", ok=True)
    assert gw.upgrade.device_occupation("dev-1")["occupied"] is False

    # 手工往快照里塞一条指向已成功批次的残留占用（模拟异常落盘）
    data = json.loads(path.read_text(encoding="utf-8"))
    data["device_occupations"] = {
        "dev-1": {"device_id": "dev-1", "campaign_id": camp.campaign_id,
                  "batch_id": batch.batch_id, "acquired_at": 1.0}}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    logger.info("场景：快照残留指向已终态批次的占用，重启后应被清掉")

    gw2 = Gateway(path)
    assert gw2.upgrade.device_occupation("dev-1")["occupied"] is False
    c2 = gw2.upgrade.create_campaign("3.0.0", ["dev-1"], batch_size=1)
    r = gw2.upgrade.upgrade_batch(c2.campaign_id, c2.batches[0].batch_id,
                                  health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
