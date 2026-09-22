"""灰度升级：分批推进、批次失败整体回滚、并发不互相覆盖、重启一致。"""
from __future__ import annotations

import threading

import pytest

from app.errors import ConflictError
from app.models import BatchState, DeviceUpgradeState
from app.service import Gateway


def _prepare(gw, n=5, version="2.0.0", batch_size=2):
    gw.upgrade.release_firmware(version)
    ids = [f"dev-{i}" for i in range(n)]
    for i in ids:
        gw.auth.register_device(i)
    return ids, gw.upgrade.create_campaign(version, ids, batch_size=batch_size)


def test_campaign_batches_succeed_in_order(gateway):
    ids, camp = _prepare(gateway, n=5, batch_size=2)
    assert len(camp.batches) == 3

    # 前序批次未成功，不能跳批
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(camp.campaign_id, camp.batches[1].batch_id)
    assert exc.value.reason == "previous_batch_not_succeeded"

    for idx, batch in enumerate(camp.batches):
        result = gateway.upgrade.upgrade_batch(
            camp.campaign_id, batch.batch_id, health_results={})
        assert result["state"] == BatchState.SUCCEEDED.value
        # 设备版本随批次推进
        for device_id in batch.device_ids:
            assert gateway.store.get_device(
                device_id).firmware_version == "2.0.0"

    # 重复升级已成功批次被拒，进度不被覆盖
    with pytest.raises(ConflictError) as exc:
        gateway.upgrade.upgrade_batch(
            camp.campaign_id, camp.batches[0].batch_id)
    assert exc.value.reason == "batch_already_succeeded"

    progress = gateway.upgrade.campaign_progress(camp.campaign_id)
    assert progress["state_counts"] == {"succeeded": 3}
    assert progress["finished"] is True


def test_batch_failure_rolls_back_only_that_batch(gateway):
    ids, camp = _prepare(gateway, n=5, batch_size=2)
    b0, b1, b2 = camp.batches

    gateway.upgrade.upgrade_batch(camp.campaign_id, b0.batch_id,
                                 health_results={})
    # 第二批次中 dev-3 健康检查失败 -> 整批回滚
    result = gateway.upgrade.upgrade_batch(
        camp.campaign_id, b1.batch_id,
        health_results={"dev-2": True, "dev-3": False})
    assert result["state"] == BatchState.ROLLED_BACK.value

    # 批次内所有设备恢复 1.0.0，包括健康的设备
    for device_id in b1.device_ids:
        device = gateway.store.get_device(device_id)
        assert device.firmware_version == "1.0.0"
        assert device.upgrade_state in (DeviceUpgradeState.FAILED,
                                        DeviceUpgradeState.ROLLED_BACK)
    # 失败设备 FAILED，随批回滚设备 ROLLED_BACK，原因可区分
    up3 = result["upgrades"]["dev-3"]
    up2 = result["upgrades"]["dev-2"]
    assert up3["state"] == DeviceUpgradeState.FAILED.value
    assert up2["state"] == DeviceUpgradeState.ROLLED_BACK.value

    # 第一批成功结果不受影响，第三批因前批未成功被挡住
    for device_id in b0.device_ids:
        assert gateway.store.get_device(
            device_id).firmware_version == "2.0.0"
    with pytest.raises(ConflictError):
        gateway.upgrade.upgrade_batch(camp.campaign_id, b2.batch_id)


def test_device_report_triggers_rollback(gateway):
    ids, camp = _prepare(gateway, n=2, batch_size=2)
    batch = camp.batches[0]
    # 开始升级但等待设备上报 -> IN_PROGRESS
    started = gateway.upgrade.upgrade_batch(
        camp.campaign_id, batch.batch_id, health_results=None)
    assert started["state"] == BatchState.IN_PROGRESS.value

    # 第一台上报成功：批次仍在进行
    pending = gateway.upgrade.report_device(
        camp.campaign_id, batch.batch_id, "dev-0", ok=True)
    assert pending["state"] == BatchState.IN_PROGRESS.value
    # 第二台上报异常：整批回滚（第一台也随之回滚）
    rolled = gateway.upgrade.report_device(
        camp.campaign_id, batch.batch_id, "dev-1", ok=False,
        message="健康检查失败")
    assert rolled["state"] == BatchState.ROLLED_BACK.value
    assert rolled["upgrades"]["dev-1"]["state"] == DeviceUpgradeState.FAILED.value
    assert rolled["upgrades"]["dev-0"]["state"] == DeviceUpgradeState.ROLLED_BACK.value
    for device_id in ("dev-0", "dev-1"):
        assert gateway.store.get_device(device_id).firmware_version == "1.0.0"


def test_concurrent_upgrade_does_not_corrupt(gateway):
    ids, camp = _prepare(gateway, n=4, batch_size=2)
    b0 = camp.batches[0].batch_id

    outcomes: list[str] = []

    def worker():
        try:
            r = gateway.upgrade.upgrade_batch(
                camp.campaign_id, b0, health_results={})
            outcomes.append(r["state"])
        except ConflictError as e:
            outcomes.append(f"rejected:{e.reason}")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个成功，其余并发请求被拒绝，设备版本只被推进一次
    assert outcomes.count("succeeded") == 1
    assert outcomes.count("rejected:batch_already_succeeded") == 7
    for device_id in camp.batches[0].device_ids:
        d = gateway.store.get_device(device_id)
        assert d.firmware_version == "2.0.0"
        assert d.upgrade_state == DeviceUpgradeState.ON_TARGET

    # 第二批也可正常继续（不受并发影响）
    b1 = camp.batches[1].batch_id
    r = gateway.upgrade.upgrade_batch(
        camp.campaign_id, b1, health_results={})
    assert r["state"] == BatchState.SUCCEEDED.value
    for i in range(4):
        assert gateway.store.get_device(f"dev-{i}").firmware_version == "2.0.0"


def test_state_survives_restart(tmp_path):
    path = tmp_path / "state.json"
    gw = Gateway(path)
    ids, camp = _prepare(gw, n=3, batch_size=2)
    b0, b1 = camp.batches
    gw.upgrade.upgrade_batch(camp.campaign_id, b0.batch_id,
                             health_results={})
    gw.upgrade.upgrade_batch(camp.campaign_id, b1.batch_id,
                             health_results={"dev-2": False})

    # 重启：新门面从同一快照恢复
    gw2 = Gateway(path)
    progress = gw2.upgrade.campaign_progress(camp.campaign_id)
    states = {b["batch_id"]: b["state"] for b in progress["batches"]}
    assert states[b0.batch_id] == BatchState.SUCCEEDED.value
    assert states[b1.batch_id] == BatchState.ROLLED_BACK.value
    assert gw2.store.get_device("dev-0").firmware_version == "2.0.0"
    assert gw2.store.get_device("dev-2").firmware_version == "1.0.0"
    # 重启后并发/重复开始仍被一致拒绝
    with pytest.raises(ConflictError) as exc:
        gw2.upgrade.upgrade_batch(camp.campaign_id, b0.batch_id)
    assert exc.value.reason == "batch_already_succeeded"
