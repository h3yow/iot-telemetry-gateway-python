"""设备占用治理：跨活动并发争抢、终态释放、暂停/继续/取消、幂等与重启恢复。"""
from __future__ import annotations

import threading

import pytest

from app.errors import ConflictError
from app.models import BatchState, CampaignState, DeviceUpgradeState
from app.service import Gateway


def _two_campaigns(gw, shared="dev-2", v1="2.0.0", v2="3.0.0"):
    """两个活动共享一台设备，其余设备各自独立。"""
    gw.upgrade.release_firmware(v1)
    gw.upgrade.release_firmware(v2)
    for d in ("dev-1", "dev-2", "dev-3"):
        gw.auth.register_device(d)
    c1 = gw.upgrade.create_campaign(v1, ["dev-1", "dev-2"], batch_size=2)
    c2 = gw.upgrade.create_campaign(v2, ["dev-2", "dev-3"], batch_size=2)
    return c1, c2


def test_concurrent_cross_campaign_contention_only_one_wins(gateway):
    c1, c2 = _two_campaigns(gateway)
    b1 = c1.batches[0].batch_id
    b2 = c2.batches[0].batch_id
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(cid, bid, tag):
        barrier.wait()
        try:
            gateway.upgrade.upgrade_batch(cid, bid)  # 停留 IN_PROGRESS
            outcomes.append(f"{tag}:started")
        except ConflictError as e:
            outcomes.append(f"{tag}:rejected:{e.reason}")

    t1 = threading.Thread(target=worker,
                          args=(c1.campaign_id, b1, "c1"))
    t2 = threading.Thread(target=worker,
                          args=(c2.campaign_id, b2, "c2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    started = [o for o in outcomes if o.endswith(":started")]
    rejected = [o for o in outcomes if o.startswith(("c1:rejected",
                                                     "c2:rejected"))]
    assert len(started) == 1
    assert len(rejected) == 1
    assert rejected[0].endswith("device_occupied_by_other_campaign")

    winner = c1 if started[0].startswith("c1") else c2
    loser = c2 if winner is c1 else c1
    loser_only = "dev-1" if loser is c1 else "dev-3"
    # 共享设备只被赢家占着
    occ = gateway.store.get_occupation("dev-2")
    assert occ is not None and occ.campaign_id == winner.campaign_id
    # 输家批次仍 PENDING，且没有改动它名下设备的版本/状态
    assert (loser.batches[0].state == BatchState.PENDING)
    assert (gateway.store.get_device(loser_only).upgrade_state
            == DeviceUpgradeState.IDLE)
    assert gateway.store.get_device("dev-2").firmware_version == "1.0.0"
    # 赢家正常推进，不被输家拖累
    wb = winner.batches[0]
    for d in wb.device_ids:
        gateway.upgrade.report_device(
            winner.campaign_id, wb.batch_id, d, ok=True)
    assert wb.state == BatchState.SUCCEEDED


def test_rejected_campaign_does_not_touch_other_devices(gateway):
    c1, c2 = _two_campaigns(gateway)
    gateway.upgrade.upgrade_batch(c1.campaign_id, c1.batches[0].batch_id)
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(
            c2.campaign_id, c2.batches[0].batch_id)
    assert exc.value.reason == "device_occupied_by_other_campaign"
    # c2 的两台设备都未被置为 UPGRADING，也没有占用记录之外的副作用
    assert gateway.store.get_occupation("dev-3") is None
    for d in ("dev-2", "dev-3"):
        dev = gateway.store.get_device(d)
        assert dev.upgrade_state == (
            DeviceUpgradeState.UPGRADING if d == "dev-2"
            else DeviceUpgradeState.IDLE)
    assert gateway.store.list_upgrade_events("dev-3") == []


def test_occupation_released_after_terminal_states(gateway):
    c1, c2 = _two_campaigns(gateway)
    gateway.upgrade.upgrade_batch(
        c1.campaign_id, c1.batches[0].batch_id,
        health_results={"dev-1": True, "dev-2": False})
    # 回滚也是终态：占用释放，设备可参加新活动
    assert gateway.store.get_occupation("dev-2") is None
    r = gateway.upgrade.upgrade_batch(
        c2.campaign_id, c2.batches[0].batch_id, health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
    assert gateway.store.get_device("dev-2").firmware_version == "3.0.0"


def test_pause_blocks_new_batches_resume_continues(gateway):
    # 两个批次：第一批成功后暂停，第二批不能启动
    gateway.upgrade.release_firmware("4.0.0")
    gateway.auth.register_device("dev-1")
    gateway.auth.register_device("dev-4")
    camp = gateway.upgrade.create_campaign(
        "4.0.0", ["dev-1", "dev-4"], batch_size=1)
    first, second = camp.batches[0], camp.batches[1]
    gateway.upgrade.upgrade_batch(
        camp.campaign_id, first.batch_id, health_results={})
    gateway.upgrade.pause_campaign(camp.campaign_id)
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(camp.campaign_id, second.batch_id)
    assert exc.value.reason == "campaign_paused"
    # 重复暂停幂等
    again = gateway.upgrade.pause_campaign(camp.campaign_id)
    assert again["state"] == CampaignState.PAUSED.value
    # 继续后从断点推进
    gateway.upgrade.resume_campaign(camp.campaign_id)
    gateway.upgrade.resume_campaign(camp.campaign_id)  # 幂等
    r = gateway.upgrade.upgrade_batch(
        camp.campaign_id, second.batch_id, health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
    assert gateway.store.get_device("dev-4").firmware_version == "4.0.0"


def test_cancel_rolls_back_in_progress_and_keeps_succeeded(gateway):
    for d in ("a", "b", "c", "d"):
        gateway.auth.register_device(d)
    gateway.upgrade.release_firmware("2.0.0")
    camp = gateway.upgrade.create_campaign(
        "2.0.0", ["a", "b", "c", "d"], batch_size=2)
    b0, b1 = camp.batches[0], camp.batches[1]
    gateway.upgrade.upgrade_batch(camp.campaign_id, b0.batch_id,
                                 health_results={})
    gateway.upgrade.upgrade_batch(camp.campaign_id, b1.batch_id)
    # 取消：b1 进行中 -> 回滚；b0 已成功 -> 不动；无 PENDING 批次
    prog = gateway.upgrade.cancel_campaign(camp.campaign_id)
    assert prog["state"] == CampaignState.CANCELLED.value
    assert prog["state_counts"] == {"succeeded": 1, "rolled_back": 1}
    assert gateway.store.get_device("a").firmware_version == "2.0.0"
    for d in ("c", "d"):
        assert gateway.store.get_device(d).firmware_version == "1.0.0"
        assert gateway.store.get_occupation(d) is None
    # 已成功的批次仍然成功，取消没有改写它
    assert b0.state == BatchState.SUCCEEDED


def test_cancel_pending_batches_marked_cancelled(gateway):
    for d in ("a", "b", "c"):
        gateway.auth.register_device(d)
    gateway.upgrade.release_firmware("2.0.0")
    camp = gateway.upgrade.create_campaign(
        "2.0.0", ["a", "b", "c"], batch_size=1)
    gateway.upgrade.upgrade_batch(
        camp.campaign_id, camp.batches[0].batch_id, health_results={})
    # b1/b2 还 PENDING 就取消
    prog = gateway.upgrade.cancel_campaign(camp.campaign_id)
    assert prog["state_counts"] == {"succeeded": 1, "cancelled": 2}
    assert gateway.store.occupations_for_campaign(camp.campaign_id) == []
    # 待开始批次设备只记一条 upgrade_cancelled，版本不变、状态不变
    hist_b = gateway.upgrade.device_upgrade_history("b")["events"]
    assert len(hist_b) == 1
    assert hist_b[0]["event_type"] == "upgrade_cancelled"
    assert hist_b[0]["from_version"] == "1.0.0"
    assert hist_b[0]["to_version"] == "1.0.0"
    assert gateway.store.get_device("b").firmware_version == "1.0.0"
    # 取消后不能再推进（批次级/活动级原因码均可区分于既有冲突）
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(
            camp.campaign_id, camp.batches[1].batch_id)
    assert exc.value.reason in ("campaign_cancelled", "batch_cancelled")


def test_cancel_is_idempotent_no_double_rollback(gateway):
    for d in ("a", "b"):
        gateway.auth.register_device(d)
    gateway.upgrade.release_firmware("2.0.0")
    camp = gateway.upgrade.create_campaign("2.0.0", ["a", "b"], batch_size=2)
    gateway.upgrade.upgrade_batch(camp.campaign_id, camp.batches[0].batch_id)
    gateway.upgrade.cancel_campaign(camp.campaign_id)
    events_before = [e.to_dict() for e in
                     gateway.store.list_upgrade_events("a")]
    version_before = gateway.store.get_device("a").firmware_version
    # 重复取消不追加记录、不改版本
    gateway.upgrade.cancel_campaign(camp.campaign_id)
    gateway.upgrade.cancel_campaign(camp.campaign_id)
    assert [e.to_dict() for e in
            gateway.store.list_upgrade_events("a")] == events_before
    assert gateway.store.get_device("a").firmware_version == version_before    # 取消后暂停/继续均被拒
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.pause_campaign(camp.campaign_id)
    assert exc.value.reason == "campaign_cancelled"
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.resume_campaign(camp.campaign_id)
    assert exc.value.reason == "campaign_cancelled"


def test_device_history_order_matches_occurrence(gateway):
    c1, c2 = _two_campaigns(gateway)
    gateway.upgrade.upgrade_batch(
        c1.campaign_id, c1.batches[0].batch_id,
        health_results={"dev-1": True, "dev-2": False})
    gateway.upgrade.upgrade_batch(
        c2.campaign_id, c2.batches[0].batch_id, health_results={})
    hist = gateway.upgrade.device_upgrade_history("dev-2")
    types = [e["event_type"] for e in hist["events"]]
    assert types == [
        "upgrade_started", "upgrade_rolled_back",
        "upgrade_started", "upgrade_succeeded"]
    campaigns = [e["campaign_id"] for e in hist["events"]]
    assert campaigns == [c1.campaign_id, c1.campaign_id,
                         c2.campaign_id, c2.campaign_id]
    assert [e["seq"] for e in hist["events"]] == [1, 2, 3, 4]
    versions = [(e["from_version"], e["to_version"])
                for e in hist["events"]]
    assert versions == [("1.0.0", "2.0.0"), ("1.0.0", "2.0.0"),
                        ("1.0.0", "3.0.0"), ("1.0.0", "3.0.0")]
    # 占用查询
    free = gateway.upgrade.device_occupation("dev-2")
    assert free["occupied"] is False


def test_occupation_and_history_survive_restart(tmp_path):
    path = tmp_path / "state.json"
    gw = Gateway(path)
    c1, c2 = _two_campaigns(gw)
    gw.upgrade.upgrade_batch(c1.campaign_id, c1.batches[0].batch_id)
    gw.upgrade.pause_campaign(c1.campaign_id)

    # 重启：占用、活动状态、版本一致
    gw2 = Gateway(path)
    occ = gw2.upgrade.device_occupation("dev-2")
    assert occ["occupied"] is True
    assert occ["occupation"]["campaign_id"] == c1.campaign_id
    assert (gw2.store.get_campaign(c1.campaign_id).state
            == CampaignState.PAUSED)
    assert gw2.store.get_device("dev-2").firmware_version == "1.0.0"
    # 重启后第二个活动仍抢不到
    with pytest.raises(ConflictError) as exc:
        gw2.upgrade.upgrade_batch(
            c2.campaign_id, c2.batches[0].batch_id, health_results={})
    assert exc.value.reason == "device_occupied_by_other_campaign"
    # 恢复后从断点继续
    gw2.upgrade.resume_campaign(c1.campaign_id)
    gw2.upgrade.report_device(
        c1.campaign_id, c1.batches[0].batch_id, "dev-1", ok=True)
    gw2.upgrade.report_device(
        c1.campaign_id, c1.batches[0].batch_id, "dev-2", ok=True)

    # 再重启：占用已释放，往来记录还在，新活动可以收走设备
    gw3 = Gateway(path)
    assert gw3.upgrade.device_occupation("dev-2")["occupied"] is False
    hist = gw3.upgrade.device_upgrade_history("dev-2")["events"]
    assert [e["event_type"] for e in hist] == [
        "upgrade_started", "upgrade_succeeded"]
    gw3.upgrade.upgrade_batch(
        c2.campaign_id, c2.batches[0].batch_id, health_results={})
    assert gw3.store.get_device("dev-2").firmware_version == "3.0.0"


def test_concurrent_http_contention_distinguishable_reason(gateway):
    from fastapi.testclient import TestClient
    from app.api import create_app
    client = TestClient(create_app(gateway))
    for d in ("d1", "d2", "d3"):
        client.post(f"/api/v1/devices/{d}/register", json={})
    client.post("/api/v1/firmware", json={"version": "2.0.0"})
    client.post("/api/v1/firmware", json={"version": "3.0.0"})
    c1 = client.post("/api/v1/campaigns", json={
        "firmware_version": "2.0.0",
        "device_ids": ["d1", "d2"], "batch_size": 2}).json()["campaign"]
    c2 = client.post("/api/v1/campaigns", json={
        "firmware_version": "3.0.0",
        "device_ids": ["d2", "d3"], "batch_size": 2}).json()["campaign"]
    url1 = (f"/api/v1/campaigns/{c1['campaign_id']}/batches/"
            f"{c1['batches'][0]['batch_id']}/upgrade")
    url2 = (f"/api/v1/campaigns/{c2['campaign_id']}/batches/"
            f"{c2['batches'][0]['batch_id']}/upgrade")
    results: list[tuple[int, str]] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def worker(url):
        barrier.wait()
        r = client.post(url, json={})
        with lock:
            reason = (r.json().get("detail", {}) or {}).get("reason", "ok")
            results.append((r.status_code, reason))

    t1 = threading.Thread(target=worker, args=(url1,))
    t2 = threading.Thread(target=worker, args=(url2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(code for code, _ in results) == [200, 409]
    loser_reason = [reason for code, reason in results if code == 409][0]
    assert loser_reason == "device_occupied_by_other_campaign"
