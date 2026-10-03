"""重启 / 原地升级恢复：旧快照占用重建、回滚往来版本落点、反复重启幂等。"""
from __future__ import annotations

import json

import pytest

from app.errors import ConflictError
from app.models import BatchState, CampaignState, DeviceUpgradeState
from app.service import Gateway


def _build_legacy_snapshot(path):
    """用当前版本造一份进行中的状态，再把 device_occupations 从快照里
    摘掉，模拟上一版（占用未持久化）原地升级后留下的快照。"""
    gw = Gateway(path)
    gw.upgrade.release_firmware("2.0.0")
    gw.upgrade.release_firmware("3.0.0")
    for d in ("dev-1", "dev-2", "dev-3"):
        gw.auth.register_device(d)
    c1 = gw.upgrade.create_campaign("2.0.0", ["dev-1", "dev-2"], batch_size=2)
    c2 = gw.upgrade.create_campaign("3.0.0", ["dev-2", "dev-3"], batch_size=2)
    gw.upgrade.upgrade_batch(c1.campaign_id, c1.batches[0].batch_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("device_occupations", None)  # 旧版快照没有这一段
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return c1, c2


def test_legacy_snapshot_in_progress_batch_still_occupies(tmp_path):
    path = tmp_path / "state.json"
    c1, c2 = _build_legacy_snapshot(path)

    # 原地升级重启：进行中批次名下的设备仍被原活动占着
    gw = Gateway(path)
    occ = gw.upgrade.device_occupation("dev-2")
    assert occ["occupied"] is True
    assert occ["occupation"]["campaign_id"] == c1.campaign_id
    assert occ["occupation"]["batch_id"] == c1.batches[0].batch_id
    occ1 = gw.upgrade.device_occupation("dev-1")
    assert occ1["occupied"] is True
    assert occ1["occupation"]["campaign_id"] == c1.campaign_id

    # 跨活动争抢仍按独立原因拒绝，且不改动任何设备
    with pytest.raises(ConflictError) as exc:
        gw.upgrade.upgrade_batch(c2.campaign_id, c2.batches[0].batch_id)
    assert exc.value.reason == "device_occupied_by_other_campaign"
    assert gw.store.get_device("dev-3").upgrade_state == DeviceUpgradeState.IDLE
    assert gw.store.get_device("dev-3").firmware_version == "1.0.0"
    assert gw.store.get_occupation("dev-3") is None
    assert gw.store.list_upgrade_events("dev-3") == []

    # 原批次继续推进不受恢复影响，走到终态后自动释放
    gw.upgrade.report_device(c1.campaign_id, c1.batches[0].batch_id,
                             "dev-1", ok=True)
    gw.upgrade.report_device(c1.campaign_id, c1.batches[0].batch_id,
                             "dev-2", ok=True)
    batch = gw.store.get_campaign(c1.campaign_id).batches[0]
    assert batch.state == BatchState.SUCCEEDED
    assert gw.upgrade.device_occupation("dev-2")["occupied"] is False

    # 释放后设备才能被新活动收走
    gw.upgrade.upgrade_batch(c2.campaign_id, c2.batches[0].batch_id,
                             health_results={})
    assert gw.store.get_device("dev-2").firmware_version == "3.0.0"


def test_restore_is_idempotent_across_repeated_restarts(tmp_path):
    path = tmp_path / "state.json"
    c1, _ = _build_legacy_snapshot(path)

    gw = Gateway(path)  # 第一次恢复：重建占用
    # 反复重启：占用不丢、不重复、不被第二个活动占上
    for _ in range(3):
        gw = Gateway(path)
        occ = gw.upgrade.device_occupation("dev-2")
        assert occ["occupied"] is True
        assert occ["occupation"]["campaign_id"] == c1.campaign_id
        # 恢复幂等：再跑一遍不再有任何变更
        assert gw.upgrade.restore_occupations() == {"rebuilt": 0,
                                                    "released": 0}
    # 同一台设备不会同时挂着两条占用
    assert len(gw.store.list_occupations()) == 2  # dev-1 / dev-2 各一条


def test_restore_releases_stale_occupation_of_terminal_batch(tmp_path):
    path = tmp_path / "state.json"
    gw = Gateway(path)
    gw.upgrade.release_firmware("2.0.0")
    gw.auth.register_device("dev-1")
    camp = gw.upgrade.create_campaign("2.0.0", ["dev-1"], batch_size=1)
    gw.upgrade.upgrade_batch(camp.campaign_id, camp.batches[0].batch_id,
                             health_results={})
    assert camp.batches[0].state == BatchState.SUCCEEDED
    # 人为制造残留：批次已成功但快照里还挂着占用（崩溃/旧版遗留）
    data = json.loads(path.read_text(encoding="utf-8"))
    data["device_occupations"] = {
        "dev-1": {"device_id": "dev-1", "campaign_id": camp.campaign_id,
                  "batch_id": camp.batches[0].batch_id, "acquired_at": 1.0}}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")

    gw2 = Gateway(path)  # 重启恢复：终态批次的残留占用必须释放
    assert gw2.upgrade.device_occupation("dev-1")["occupied"] is False
    # 释放后能正常参加后面的活动
    gw2.upgrade.release_firmware("3.0.0")
    c2 = gw2.upgrade.create_campaign("3.0.0", ["dev-1"], batch_size=1)
    gw2.upgrade.upgrade_batch(c2.campaign_id, c2.batches[0].batch_id,
                              health_results={})
    assert gw2.store.get_device("dev-1").firmware_version == "3.0.0"


def _assert_history_lands_on_current(gw, device_id):
    """终态事件的版本链首尾相接，且最终落在设备当前版本上。"""
    events = gw.upgrade.device_upgrade_history(device_id)["events"]
    terminal = [e for e in events if e["event_type"] != "upgrade_started"]
    assert terminal, "至少有一条终态往来"
    for prev, nxt in zip(terminal, terminal[1:]):
        assert prev["to_version"] == nxt["from_version"]
    assert (terminal[-1]["to_version"]
            == gw.store.get_device(device_id).firmware_version)
    return terminal


def test_rollback_history_lands_on_real_version_all_paths(tmp_path):
    """健康失败 / 人工回滚 / 取消连带，三种回滚的往来落点都是真实版本。"""
    path = tmp_path / "state.json"
    gw = Gateway(path)
    gw.upgrade.release_firmware("2.0.0")
    for d in ("dev-h", "dev-m", "dev-c"):
        gw.auth.register_device(d)
    # 健康检查失败
    ch = gw.upgrade.create_campaign("2.0.0", ["dev-h"], batch_size=1)
    gw.upgrade.upgrade_batch(ch.campaign_id, ch.batches[0].batch_id,
                             health_results={"dev-h": False})
    # 人工回滚
    cm = gw.upgrade.create_campaign("2.0.0", ["dev-m"], batch_size=1)
    gw.upgrade.upgrade_batch(cm.campaign_id, cm.batches[0].batch_id)
    gw.upgrade.rollback_batch(cm.campaign_id, cm.batches[0].batch_id)
    # 活动取消连带回滚
    cc = gw.upgrade.create_campaign("2.0.0", ["dev-c"], batch_size=1)
    gw.upgrade.upgrade_batch(cc.campaign_id, cc.batches[0].batch_id)
    gw.upgrade.cancel_campaign(cc.campaign_id)

    for d in ("dev-h", "dev-m", "dev-c"):
        assert gw.store.get_device(d).firmware_version == "1.0.0"
        terminal = _assert_history_lands_on_current(gw, d)
        rollback = [e for e in terminal
                    if e["event_type"] == "upgrade_rolled_back"]
        assert len(rollback) == 1
        # 回滚往来：从目标版本退回原版本，落点是设备真实所在版本
        assert rollback[0]["from_version"] == "2.0.0"
        assert rollback[0]["to_version"] == "1.0.0"

    # 重启以后往来记录与版本落点仍然一致
    gw2 = Gateway(path)
    for d in ("dev-h", "dev-m", "dev-c"):
        _assert_history_lands_on_current(gw2, d)


def test_cancel_after_restart_releases_and_stays_idempotent(tmp_path):
    path = tmp_path / "state.json"
    c1, _ = _build_legacy_snapshot(path)

    gw = Gateway(path)  # 恢复占用后取消：进行中批次回滚并释放
    prog = gw.upgrade.cancel_campaign(c1.campaign_id)
    assert prog["state"] == CampaignState.CANCELLED.value
    assert gw.upgrade.device_occupation("dev-2")["occupied"] is False
    assert gw.store.get_device("dev-2").firmware_version == "1.0.0"
    events_before = [e.to_dict()
                     for e in gw.store.list_upgrade_events("dev-2")]

    # 反复重启 + 重复取消：不重复回滚、不追加记录、不改版本
    for _ in range(2):
        gw = Gateway(path)
        gw.upgrade.cancel_campaign(c1.campaign_id)
        assert gw.upgrade.device_occupation("dev-2")["occupied"] is False
        assert gw.store.get_device("dev-2").firmware_version == "1.0.0"
        assert [e.to_dict()
                for e in gw.store.list_upgrade_events("dev-2")] == events_before
