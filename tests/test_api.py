"""HTTP 入口与门面结论一致，拒绝原因可区分。"""
from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import router


def _client(gateway) -> TestClient:
    from app.api import create_app
    return TestClient(create_app(gateway))


def test_http_full_flow(gateway, registered):
    client = _client(gateway)
    device_id, token = registered
    headers = {"Authorization": f"Bearer {token}"}

    # 遥测受理 + 幂等重放（原因码可区分）
    r = client.post(f"/api/v1/devices/{device_id}/telemetry",
                    json={"seq": 1, "ts": 100.0,
                          "metrics": {"temp": 36.5}}, headers=headers)
    assert r.status_code == 200 and r.json()["reason"] == "accepted"
    r = client.post(f"/api/v1/devices/{device_id}/telemetry",
                    json={"seq": 1, "ts": 100.0,
                          "metrics": {"temp": 36.5}}, headers=headers)
    assert r.status_code == 200 and r.json()["reason"] == "duplicate_replay"


def test_http_auth_failures_distinguishable(gateway, registered):
    client = _client(gateway)
    device_id, token = registered

    # 未注册设备
    r = client.post("/api/v1/devices/ghost/telemetry",
                    json={"seq": 1, "ts": 1.0, "metrics": {"a": 1}},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "device_not_registered"

    # 无凭据
    r = client.post(f"/api/v1/devices/{device_id}/telemetry",
                    json={"seq": 2, "ts": 1.0, "metrics": {"a": 1}})
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "credential_not_found"

    # 吊销后旧凭据立即失效
    cred_id = gateway.auth.authenticate(device_id, token)[2].credential_id
    r = client.post(
        f"/api/v1/devices/{device_id}/credentials/{cred_id}/revoke", json={})
    assert r.status_code == 200
    r = client.post(f"/api/v1/devices/{device_id}/telemetry",
                    json={"seq": 3, "ts": 1.0, "metrics": {"a": 1}},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "credential_revoked"


def test_http_invalid_payload_and_quota(gateway, tmp_path):
    from app.service import Gateway
    gw = Gateway(tmp_path / "q.json",
                 quota_window_seconds=60, quota_max_per_window=1)
    gw.auth.register_device("d")
    token = gw.auth.issue_credential("d")["token"]
    client = _client(gw)
    url = "/api/v1/devices/d/telemetry"
    headers = {"Authorization": f"Bearer {token}"}

    # 非法 JSON
    r = client.post(url, content=b"{not-json",
                    headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_payload"

    # 非法字段
    r = client.post(url, json={"seq": "x", "ts": 1, "metrics": {"a": 1}},
                    headers=headers)
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "invalid_payload"

    # 配额
    assert client.post(url, json={"seq": 1, "ts": 1, "metrics": {"a": 1}},
                       headers=headers).status_code == 200
    r = client.post(url, json={"seq": 2, "ts": 2, "metrics": {"a": 1}},
                    headers=headers)
    assert r.status_code == 429
    assert r.json()["detail"]["reason"] == "quota_exceeded"


def test_http_upgrade_with_rollback(gateway):
    client = _client(gateway)
    for d in ("d1", "d2"):
        client.post(f"/api/v1/devices/{d}/register", json={})
    client.post("/api/v1/firmware", json={"version": "2.0.0"})
    r = client.post("/api/v1/campaigns",
                    json={"firmware_version": "2.0.0",
                          "device_ids": ["d1", "d2"], "batch_size": 2})
    camp = r.json()["campaign"]
    cid, bid = camp["campaign_id"], camp["batches"][0]["batch_id"]

    # 批次内 d2 健康检查失败 -> 整批回滚
    r = client.post(f"/api/v1/campaigns/{cid}/batches/{bid}/upgrade",
                    json={"health_results": {"d1": True, "d2": False}})
    body = r.json()["batch"]
    assert body["state"] == "rolled_back"
    assert body["upgrades"]["d1"]["state"] == "rolled_back"
    assert body["upgrades"]["d2"]["state"] == "failed"
    # 设备当前版本未被覆盖
    assert client.get("/api/v1/devices/d1").json()["device"][
        "firmware_version"] == "1.0.0"
    assert client.get("/api/v1/devices/d2").json()["device"][
        "firmware_version"] == "1.0.0"

    # 重复开始回滚态批次被拒，原因可区分
    r = client.post(f"/api/v1/campaigns/{cid}/batches/{bid}/upgrade",
                    json={"health_results": {}})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "batch_rolled_back"


def test_http_concurrent_upgrade(gateway):
    client = _client(gateway)
    for d in ("d1", "d2"):
        client.post(f"/api/v1/devices/{d}/register", json={})
    client.post("/api/v1/firmware", json={"version": "9.9.9"})
    camp = client.post("/api/v1/campaigns", json={
        "firmware_version": "9.9.9",
        "device_ids": ["d1", "d2"], "batch_size": 2}).json()["campaign"]
    cid, bid = camp["campaign_id"], camp["batches"][0]["batch_id"]
    url = f"/api/v1/campaigns/{cid}/batches/{bid}/upgrade"

    statuses: list[int] = []
    lock = threading.Lock()

    def worker():
        r = client.post(url, json={"health_results": {}})
        with lock:
            statuses.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses.count(200) == 1
    assert statuses.count(409) == 5


def _make_campaign(client, version, devices, batch_size=2):
    for d in devices:
        client.post(f"/api/v1/devices/{d}/register", json={})
    client.post("/api/v1/firmware", json={"version": version})
    r = client.post("/api/v1/campaigns",
                    json={"firmware_version": version,
                          "device_ids": list(devices),
                          "batch_size": batch_size})
    return r.json()["campaign"]


def test_http_cross_campaign_occupancy_distinguishable(gateway):
    client = _client(gateway)
    c1 = _make_campaign(client, "2.0.0", ["g1", "g2"])
    c2 = _make_campaign(client, "3.0.0", ["g1", "g2"])
    # 活动一进行中占用设备
    r = client.post(
        f"/api/v1/campaigns/{c1['campaign_id']}/batches/"
        f"{c1['batches'][0]['batch_id']}/upgrade", json={})
    assert r.status_code == 200
    # 活动二争抢被拒：独立原因 device_occupied
    r = client.post(
        f"/api/v1/campaigns/{c2['campaign_id']}/batches/"
        f"{c2['batches'][0]['batch_id']}/upgrade",
        json={"health_results": {}})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "device_occupied"
    # 占用查询
    r = client.get("/api/v1/devices/g1/occupancy")
    body = r.json()
    assert body["occupied"] is True
    assert body["campaign_id"] == c1["campaign_id"]


def test_http_pause_resume_cancel_and_history(gateway):
    client = _client(gateway)
    camp = _make_campaign(client, "2.0.0",
                          ["h1", "h2", "h3", "h4"], batch_size=2)
    cid = camp["campaign_id"]
    b0, b1 = camp["batches"][0]["batch_id"], camp["batches"][1]["batch_id"]
    client.post(f"/api/v1/campaigns/{cid}/batches/{b0}/upgrade",
                json={"health_results": {}})

    r = client.post(f"/api/v1/campaigns/{cid}/pause", json={})
    assert r.status_code == 200 and r.json()["state"] == "paused"
    # 暂停后推进被拒
    r = client.post(f"/api/v1/campaigns/{cid}/batches/{b1}/upgrade",
                    json={"health_results": {}})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "campaign_paused"
    # 继续后可推进
    r = client.post(f"/api/v1/campaigns/{cid}/resume", json={})
    assert r.status_code == 200 and r.json()["state"] == "active"
    r = client.post(f"/api/v1/campaigns/{cid}/batches/{b1}/upgrade",
                    json={"health_results": {}})
    assert r.status_code == 200
    # 全部批次终态后重复取消幂等
    r1 = client.post(f"/api/v1/campaigns/{cid}/cancel", json={})
    r2 = client.post(f"/api/v1/campaigns/{cid}/cancel", json={})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["state"] == r2.json()["state"] == "cancelled"

    # 升级往来可查、顺序正确
    r = client.get("/api/v1/devices/h1/upgrade-history")
    kinds = [e["kind"] for e in r.json()["events"]]
    assert kinds == ["upgrade_started", "upgrade_succeeded"]
    # 未注册设备查询 404
    assert client.get("/api/v1/devices/ghost/occupancy").status_code == 404
