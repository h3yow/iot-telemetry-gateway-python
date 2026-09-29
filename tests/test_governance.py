"""设备占用治理：跨活动并发争抢、终态释放、暂停/继续/取消、升级往来、重启恢复。

并发用例均以真实多线程同时打进来验证（非顺序模拟）。
"""
from __future__ import annotations

import threading

import pytest

from app.errors import ConflictError, NotFoundError
from app.models import BatchState, CampaignState, DeviceUpgradeState
from app.service import Gateway


def _prep(gw, devices=("dev-0", "dev-1"), versions=("2.0.0", "3.0.0"),
          batch_size=2):
    """注册设备、发布两个目标版本、创建两个针对同一批设备的活动。"""
    for v in versions:
        gw.upgrade.release_firmware(v)
    for d in devices:
        gw.auth.register_device(d)
    camps = [gw.upgrade.create_campaign(v, list(devices),
                                        batch_size=batch_size)
             for v in versions]
    return camps


def test_concurrent_cross_campaign_only_one_wins(gateway):
    c1, c2 = _prep(gateway)
    b1, b2 = c1.batches[0].batch_id, c2.batches[0].batch_id

    # 先让活动一占用设备（批次进入 IN_PROGRESS，版本还停在 1.0.0）
    gateway.upgrade.upgrade_batch(c1.campaign_id, b1)
    assert gateway.upgrade.device_occupancy("dev-0") == {
        "device_id": "dev-0", "occupied": True,
        "campaign_id": c1.campaign_id, "batch_id": b1,
        "occupied_at": gateway.upgrade.device_occupancy("dev-0")[
            "occupied_at"]}

    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # 尽量同时打进来
        try:
            gateway.upgrade.upgrade_batch(
                c2.campaign_id, b2, health_results={})
            outcomes.append("won")
        except ConflictError as e:
            outcomes.append(e.reason)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 活动二的并发争抢全部被拒，原因独立、与既有拒绝码区分
    assert outcomes == ["device_occupied"] * 8
    # 被拒一方没有改动设备版本，也没有推进自己的批次
    assert gateway.store.get_device("dev-0").firmware_version == "1.0.0"
    assert gateway.store.get_device("dev-1").firmware_version == "1.0.0"
    assert c2.campaign_id in {
        c.campaign_id for c in gateway.store.list_campaigns()}
    assert gateway.store.get_campaign(
        c2.campaign_id).batches[0].state == BatchState.PENDING
    # 活动一仍正常推进，不受拖累
    assert gateway.store.get_campaign(
        c1.campaign_id).batches[0].state == BatchState.IN_PROGRESS


def test_concurrent_start_same_devices_exactly_one_campaign(gateway):
    """两个活动的批次开始动作同时到达同一批空闲设备，恰好一个成功占住。

    使用 health_results=None 让批次停在 IN_PROGRESS（占用挂着），
    从而真正模拟「同时打进来、目标版本不同」的争用。
    """
    c1, c2 = _prep(gateway)
    b1, b2 = c1.batches[0].batch_id, c2.batches[0].batch_id
    winners: list[str] = []
    rejected: list[str] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker(cid, bid):
        barrier.wait()
        try:
            r = gateway.upgrade.upgrade_batch(cid, bid, health_results=None)
            assert r["state"] == BatchState.IN_PROGRESS.value
            with lock:
                winners.append(cid)
        except ConflictError as e:
            with lock:
                rejected.append(e.reason)

    threads = []
    for cid, bid in ((c1.campaign_id, b1), (c2.campaign_id, b2)):
        for _ in range(4):
            threads.append(threading.Thread(
                target=worker, args=(cid, bid)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个活动抢到（其 4 个并发请求中只有 1 个真正开始）
    assert len(winners) == 1
    winner_cid = winners[0]
    loser_cid = c2.campaign_id if winner_cid == c1.campaign_id else c1.campaign_id
    from collections import Counter
    counts = Counter(rejected)
    # 抢到活动的其余 3 个重复开始：batch_in_progress；落败活动 4 个：device_occupied
    assert counts["device_occupied"] == 4
    assert counts["batch_in_progress"] == 3
    # 设备版本没动（批次进行中），占用唯一归属获胜活动
    for d in ("dev-0", "dev-1"):
        dev = gateway.store.get_device(d)
        assert dev.firmware_version == "1.0.0"
        assert gateway.upgrade.device_occupancy(
            d)["campaign_id"] == winner_cid
    # 落败活动批次保持 PENDING
    assert gateway.store.get_campaign(
        loser_cid).batches[0].state == BatchState.PENDING


def test_occupancy_released_on_success_and_rollback(gateway):
    c1, c2 = _prep(gateway)
    # 活动一第一批成功 -> 占用释放
    gateway.upgrade.upgrade_batch(
        c1.campaign_id, c1.batches[0].batch_id, health_results={})
    assert gateway.upgrade.device_occupancy("dev-0")["occupied"] is False
    # 释放后设备可参加新活动（活动二接手同一批设备）
    r = gateway.upgrade.upgrade_batch(
        c2.campaign_id, c2.batches[0].batch_id,
        health_results={"dev-0": False, "dev-1": True})
    assert r["state"] == BatchState.ROLLED_BACK.value
    # 回滚终态也释放占用，版本恢复升级前（活动一留下的 2.0.0）
    assert gateway.upgrade.device_occupancy("dev-0")["occupied"] is False
    assert gateway.store.get_device("dev-0").firmware_version == "2.0.0"
    assert gateway.store.get_device("dev-1").firmware_version == "2.0.0"


def test_pause_blocks_new_batches_resume_continues(gateway):
    camps = _prep(gateway, devices=tuple(f"dev-{i}" for i in range(5)),
                  versions=("2.0.0",), batch_size=2)
    camp = camps[0]
    b0, b1 = camp.batches[0], camp.batches[1]
    gateway.upgrade.upgrade_batch(camp.campaign_id, b0.batch_id,
                                  health_results={})

    progress = gateway.upgrade.pause_campaign(camp.campaign_id)
    assert progress["state"] == CampaignState.PAUSED.value
    # 重复暂停幂等
    assert gateway.upgrade.pause_campaign(
        camp.campaign_id)["state"] == CampaignState.PAUSED.value
    # 暂停后不能推进新批次
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(camp.campaign_id, b1.batch_id)
    assert exc.value.reason == "campaign_paused"
    # 暂停已取消活动报错
    gateway.upgrade.cancel_campaign(camp.campaign_id)
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.pause_campaign(camp.campaign_id)
    assert exc.value.reason == "campaign_cancelled"


def test_resume_from_breakpoint(gateway):
    camps = _prep(gateway, devices=tuple(f"dev-{i}" for i in range(4)),
                  versions=("2.0.0",), batch_size=2)
    camp = camps[0]
    b0, b1 = camp.batches[0], camp.batches[1]
    gateway.upgrade.upgrade_batch(camp.campaign_id, b0.batch_id,
                                  health_results={})
    gateway.upgrade.pause_campaign(camp.campaign_id)
    # 继续后从原断点（b0 已成功 -> b1）接着走
    progress = gateway.upgrade.resume_campaign(camp.campaign_id)
    assert progress["state"] == CampaignState.ACTIVE.value
    # 重复继续幂等
    assert gateway.upgrade.resume_campaign(
        camp.campaign_id)["state"] == CampaignState.ACTIVE.value
    r = gateway.upgrade.upgrade_batch(camp.campaign_id, b1.batch_id,
                                      health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
    # 继续已取消活动报错
    c2 = _prep(gateway, devices=("x-0", "x-1"), versions=("4.0.0",))[0]
    gateway.upgrade.cancel_campaign(c2.campaign_id)
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.resume_campaign(c2.campaign_id)
    assert exc.value.reason == "campaign_cancelled"


def test_cancel_releases_inflight_and_keeps_succeeded(gateway):
    camps = _prep(gateway, devices=tuple(f"dev-{i}" for i in range(5)),
                  versions=("2.0.0",), batch_size=2)
    camp = camps[0]
    b0, b1, b2 = camp.batches
    gateway.upgrade.upgrade_batch(camp.campaign_id, b0.batch_id,
                                  health_results={})  # 成功，保留
    gateway.upgrade.upgrade_batch(camp.campaign_id, b1.batch_id)  # 进行中

    progress = gateway.upgrade.cancel_campaign(camp.campaign_id)
    counts = progress["state_counts"]
    assert counts["succeeded"] == 1
    assert counts["rolled_back"] == 1
    assert counts["cancelled"] == 1
    # 已成功部分不动；进行中回滚到升级前并释放；未开始取消且不动设备
    assert gateway.store.get_device("dev-0").firmware_version == "2.0.0"
    assert gateway.store.get_device("dev-2").firmware_version == "1.0.0"
    assert gateway.store.get_device("dev-4").firmware_version == "1.0.0"
    assert gateway.store.get_device(
        "dev-4").upgrade_state == DeviceUpgradeState.IDLE
    for d in ("dev-2", "dev-3"):
        assert gateway.upgrade.device_occupancy(d)["occupied"] is False

    # 重复取消幂等：不重复回滚、版本不再变化
    again = gateway.upgrade.cancel_campaign(camp.campaign_id)
    assert again["state_counts"] == counts
    assert gateway.store.get_device("dev-2").firmware_version == "1.0.0"
    # 取消后任何推进都被拒，原因可区分
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(camp.campaign_id, b2.batch_id)
    assert exc.value.reason == "batch_cancelled"
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(camp.campaign_id, b1.batch_id)
    assert exc.value.reason == "batch_rolled_back"

    # 释放后的设备可以参加全新活动
    gateway.upgrade.release_firmware("5.0.0")
    fresh = gateway.upgrade.create_campaign(
        "5.0.0", ["dev-2", "dev-3", "dev-4"], batch_size=3)
    r = gateway.upgrade.upgrade_batch(
        fresh.campaign_id, fresh.batches[0].batch_id, health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
    for d in ("dev-2", "dev-3", "dev-4"):
        assert gateway.store.get_device(d).firmware_version == "5.0.0"


def test_upgrade_history_order_and_content(gateway):
    c1, c2 = _prep(gateway)
    b0 = c1.batches[0].batch_id
    gateway.upgrade.upgrade_batch(c1.campaign_id, b0, health_results={})
    gateway.upgrade.upgrade_batch(
        c2.campaign_id, c2.batches[0].batch_id,
        health_results={"dev-0": False})

    history = gateway.upgrade.device_upgrade_history("dev-0")
    assert history["current_version"] == "2.0.0"
    kinds = [(e["kind"], e["from_version"], e["to_version"],
              e["campaign_id"]) for e in history["events"]]
    assert kinds == [
        ("upgrade_started", "1.0.0", "2.0.0", c1.campaign_id),
        ("upgrade_succeeded", "1.0.0", "2.0.0", c1.campaign_id),
        ("upgrade_started", "2.0.0", "3.0.0", c2.campaign_id),
        ("upgrade_rolled_back", "3.0.0", "2.0.0", c2.campaign_id),
    ]
    # 顺序与实际发生顺序一致：seq 全局单调递增
    seqs = [e["seq"] for e in history["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == 4
    # 未注册设备查询 404
    with pytest.raises(NotFoundError):
        gateway.upgrade.device_upgrade_history("ghost")
    with pytest.raises(NotFoundError):
        gateway.upgrade.device_occupancy("ghost")


def test_occupancy_and_history_survive_restart(tmp_path):
    path = tmp_path / "state.json"
    gw = Gateway(path)
    c1, c2 = _prep(gw)
    # 活动一进行中（占用挂着）时重启
    gw.upgrade.upgrade_batch(c1.campaign_id, c1.batches[0].batch_id)
    gw.upgrade.pause_campaign(c1.campaign_id)

    gw2 = Gateway(path)
    # 占用关系与重启前一致：设备仍属活动一
    occ = gw2.upgrade.device_occupancy("dev-0")
    assert occ["occupied"] is True
    assert occ["campaign_id"] == c1.campaign_id
    assert occ["batch_id"] == c1.batches[0].batch_id
    # 活动状态保持暂停
    assert gw2.upgrade.campaign_progress(
        c1.campaign_id)["state"] == CampaignState.PAUSED.value
    # 重启后第二个活动不能把设备收走
    with pytest.raises(ConflictError) as exc:
        gw2.upgrade.upgrade_batch(
            c2.campaign_id, c2.batches[0].batch_id, health_results={})
    assert exc.value.reason == "device_occupied"
    # 继续后活动一仍可从断点推进
    gw2.upgrade.resume_campaign(c1.campaign_id)
    r = gw2.upgrade.report_device(
        c1.campaign_id, c1.batches[0].batch_id, "dev-0", True)
    r = gw2.upgrade.report_device(
        c1.campaign_id, c1.batches[0].batch_id, "dev-1", True)
    assert r["state"] == BatchState.SUCCEEDED.value
    # 往来记录重启后仍在且顺序不乱
    history = gw2.upgrade.device_upgrade_history("dev-0")
    assert [e["kind"] for e in history["events"]] == [
        "upgrade_started", "upgrade_succeeded"]

    # 已取消活动重启后仍为取消态、占用不残留
    gw2.upgrade.upgrade_batch(
        c2.campaign_id, c2.batches[0].batch_id)  # 设备已释放，可占用
    gw2.upgrade.cancel_campaign(c2.campaign_id)
    Gateway(path)  # 再重启一次
    gw3 = Gateway(path)
    assert gw3.upgrade.campaign_progress(
        c2.campaign_id)["state"] == CampaignState.CANCELLED.value
    assert gw3.upgrade.device_occupancy("dev-0")["occupied"] is False
