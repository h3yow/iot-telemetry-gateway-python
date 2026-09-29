"""本地模拟器（CLI 入口）。

在不依赖任何外部服务的前提下，完整走一遍：
  注册设备 -> 签发凭据 -> 鉴权遥测（正常/乱序/重复/回跳/非法/超限/吊销）
  -> 发布固件 -> 两批次灰度（成功批 + 失败回滚批）
  -> 跨活动设备占用争抢 -> 暂停/继续 -> 取消（进行中回滚/待开始取消）
  -> 重启一致性检查。

运行：
    uv run python -m app.simulator                 # 纯内存
    uv run python -m app.simulator --store s.json  # 带持久化快照
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from app.errors import GatewayError
from app.service import Gateway


def _guard(fn, *args, **kwargs) -> dict[str, Any]:
    """统一捕获，输出可区分的结论。"""
    try:
        result = fn(*args, **kwargs)
        return {"ok": True, "result": result}
    except GatewayError as exc:
        return {"ok": False, "reason": exc.reason, "message": exc.message}


def run_demo(store_path: str | None = None) -> dict[str, Any]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s")

    # 演示从干净状态开始；重启一致性检查在结尾用同一快照新建 Gateway
    if store_path:
        from pathlib import Path
        Path(store_path).unlink(missing_ok=True)
    gw = Gateway(store_path, quota_window_seconds=60,
                 quota_max_per_window=5)
    report: dict[str, Any] = {"steps": []}

    def step(name: str, outcome: dict) -> dict:
        report["steps"].append({"step": name, **outcome})
        return outcome

    # 1) 注册并签发凭据
    step("register", _guard(gw.auth.register_device, "sensor-a"))
    issued = gw.auth.issue_credential("sensor-a")
    token = issued["token"]
    step("issue_credential", {"ok": True,
                              "credential_id": issued["credential_id"]})

    # 2) 遥测：正常、乱序、时间戳回跳、重复重放
    push = lambda seq, ts: gw.authenticated_telemetry(
        "sensor-a", token,
        {"seq": seq, "ts": ts, "metrics": {"temp": 20.0 + seq}})
    step("telemetry_seq1_ts100", _guard(push, 1, 100.0))
    step("telemetry_seq3_ts300", _guard(push, 3, 300.0))
    step("telemetry_seq2_ts200_out_of_order", _guard(push, 2, 200.0))
    step("telemetry_seq4_ts150_ts_regression", _guard(push, 4, 150.0))
    step("telemetry_seq1_replay", _guard(push, 1, 100.0))

    # 3) 非法格式（拒绝，不计数）；此时窗口已受理 4 条
    step("telemetry_invalid", _guard(
        gw.authenticated_telemetry, "sensor-a", token,
        {"seq": "bad", "ts": 1, "metrics": {"x": 1}}))

    # 4) 鉴权：伪造令牌 vs 吊销后旧凭据；轮换出新凭据后新凭据可用
    from app.auth import encode_token
    forged = encode_token(issued["credential_id"], "wrong-secret")
    step("auth_wrong_secret", _guard(
        gw.authenticated_telemetry, "sensor-a", forged,
        {"seq": 9, "ts": 1, "metrics": {"x": 1}}))
    gw.auth.revoke_credential(issued["credential_id"])
    step("auth_after_revoke", _guard(
        gw.authenticated_telemetry, "sensor-a", token,
        {"seq": 10, "ts": 1, "metrics": {"x": 1}}))
    new_issued = gw.auth.issue_credential("sensor-a")
    # 新凭据受理第 5 条，恰好打满窗口
    step("auth_new_credential_seq5", _guard(
        gw.authenticated_telemetry, "sensor-a", new_issued["token"],
        {"seq": 5, "ts": 500.0, "metrics": {"temp": 25.0}}))

    # 5) 窗口已满：第 6 条被配额拒绝（原因与非法、重放均可区分）
    step("telemetry_quota_attempt_seq6", _guard(
        gw.authenticated_telemetry, "sensor-a", new_issued["token"],
        {"seq": 6, "ts": 600.0, "metrics": {"temp": 26.0}}))
    report["telemetry_stats"] = gw.telemetry.stats("sensor-a")

    # 6) 固件灰度：4 台设备 2 批次，第一批成功，第二批失败整批回滚
    for d in ("sensor-b", "sensor-c", "sensor-d", "sensor-e"):
        gw.auth.register_device(d)
    gw.upgrade.release_firmware("2.0.0")
    campaign = gw.upgrade.create_campaign(
        "2.0.0",
        ["sensor-b", "sensor-c", "sensor-d", "sensor-e"],
        batch_size=2)
    cid = campaign.campaign_id
    b0, b1 = campaign.batches[0].batch_id, campaign.batches[1].batch_id
    step("upgrade_batch0_ok",
         {"ok": True,
          "batch": gw.upgrade.upgrade_batch(cid, b0, health_results={})})
    step("upgrade_batch1_fail_rolls_back",
         {"ok": True,
          "batch": gw.upgrade.upgrade_batch(
              cid, b1,
              health_results={"sensor-d": True, "sensor-e": False})})
    step("concurrent_upgrade_rejected", _guard(
        gw.upgrade.upgrade_batch, cid, b0, health_results={}))
    report["campaign_progress"] = gw.upgrade.campaign_progress(cid)
    report["versions"] = {
        d: gw.store.get_device(d).firmware_version
        for d in ("sensor-b", "sensor-c", "sensor-d", "sensor-e")}

    # 7) 设备占用治理：两个活动共享 sensor-g，并发争抢只有一个成功
    for d in ("sensor-f", "sensor-g", "sensor-h", "sensor-i"):
        gw.auth.register_device(d)
    gw.upgrade.release_firmware("3.0.0")
    camp_a = gw.upgrade.create_campaign(
        "2.0.0", ["sensor-f", "sensor-g", "sensor-i"], batch_size=2)
    camp_b = gw.upgrade.create_campaign(
        "3.0.0", ["sensor-g", "sensor-h"], batch_size=2)
    step("occupation_campaign_a_start",
         {"ok": True, "batch": gw.upgrade.upgrade_batch(
             camp_a.campaign_id, camp_a.batches[0].batch_id)})
    step("occupation_campaign_b_rejected", _guard(
        gw.upgrade.upgrade_batch,
        camp_b.campaign_id, camp_b.batches[0].batch_id))
    report["occupation_sensor_g"] = gw.upgrade.device_occupation("sensor-g")
    # 第一批从断点逐台上报成功后暂停：第二批 PENDING 被 campaign_paused 挡住
    step("campaign_a_report_f",
         {"ok": True, "batch": gw.upgrade.report_device(
             camp_a.campaign_id, camp_a.batches[0].batch_id,
             "sensor-f", ok=True)})
    step("campaign_a_report_g_succeeds",
         {"ok": True, "batch": gw.upgrade.report_device(
             camp_a.campaign_id, camp_a.batches[0].batch_id,
             "sensor-g", ok=True)})
    step("campaign_a_pause",
         {"ok": True, **gw.upgrade.pause_campaign(camp_a.campaign_id)})
    step("campaign_a_pause_again_idempotent",
         {"ok": True, **gw.upgrade.pause_campaign(camp_a.campaign_id)})
    step("campaign_a_paused_new_batch_rejected", _guard(
        gw.upgrade.upgrade_batch,
        camp_a.campaign_id, camp_a.batches[1].batch_id))
    step("campaign_a_resume",
         {"ok": True, **gw.upgrade.resume_campaign(camp_a.campaign_id)})
    step("campaign_a_second_batch",
         {"ok": True, "batch": gw.upgrade.upgrade_batch(
             camp_a.campaign_id, camp_a.batches[1].batch_id,
             health_results={})})
    # 释放后活动 B 可以收走 sensor-g
    step("occupation_released_then_campaign_b",
         {"ok": True, "batch": gw.upgrade.upgrade_batch(
             camp_b.campaign_id, camp_b.batches[0].batch_id,
             health_results={})})

    # 8) 取消第三个活动：进行中的批次回滚、待开始批次取消、已成功批次不动
    for d in ("sensor-j", "sensor-k", "sensor-l"):
        gw.auth.register_device(d)
    gw.upgrade.release_firmware("4.0.0")
    camp_c = gw.upgrade.create_campaign(
        "4.0.0", ["sensor-j", "sensor-k", "sensor-l"], batch_size=1)
    gw.upgrade.upgrade_batch(camp_c.campaign_id,
                             camp_c.batches[0].batch_id, health_results={})
    gw.upgrade.upgrade_batch(camp_c.campaign_id,
                             camp_c.batches[1].batch_id)
    step("campaign_c_cancel",
         {"ok": True, **gw.upgrade.cancel_campaign(camp_c.campaign_id)})
    step("campaign_c_cancel_again_idempotent",
         {"ok": True, **gw.upgrade.cancel_campaign(camp_c.campaign_id)})
    report["upgrade_history_sensor_g"] = gw.upgrade.device_upgrade_history(
        "sensor-g")
    report["occupation_sensor_g_after"] = gw.upgrade.device_occupation(
        "sensor-g")

    # 9) 重启一致性（仅在使用快照路径时有意义）
    if store_path:
        gw2 = Gateway(store_path)
        report["after_restart"] = {
            "campaign_progress": gw2.upgrade.campaign_progress(cid),
            "occupation_sensor_g": gw2.upgrade.device_occupation("sensor-g"),
            "history_sensor_k": gw2.upgrade.device_upgrade_history(
                "sensor-k"),
        }

    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="IoT 网关本地模拟器")
    parser.add_argument("--store", default=None,
                        help="持久化 JSON 快照路径（演示重启一致性）")
    args = parser.parse_args(argv)
    print(json.dumps(run_demo(args.store), ensure_ascii=False, indent=2,
                     default=str))


if __name__ == "__main__":
    main()
