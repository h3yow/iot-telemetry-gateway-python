"""本地端到端演示（CLI 入口）。

不依赖任何外部服务：在进程内模拟 3 台设备、网关鉴权、遥测上报、
固件镜像与两批次灰度升级，第二批次注入健康检查失败以演示整体回滚。
运行：``uv run python scripts/demo.py``
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.errors import GatewayError
from gateway.service import GatewayService

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", ".demo-state.json")


def _hr(title: str) -> None:
    print("\n" + "=" * 12 + f" {title} " + "=" * 12)


def main() -> None:
    if os.path.exists(STATE_FILE):
        os.unlink(STATE_FILE)
    svc = GatewayService(STATE_FILE, telemetry_quota=5)

    _hr("1. 注册设备与签发凭据")
    creds: dict[str, dict] = {}
    for did in ("dev-A", "dev-B", "dev-C"):
        creds[did] = svc.register_device(did)
        print(f"设备 {did} 注册成功，凭据={creds[did]['credential_id']}")

    _hr("2. 鉴权拒绝原因可区分")
    probes = [
        ("dev-A", None, None, "无凭据"),
        ("unknown", "x", "y", "未注册设备"),
        ("dev-A", "no-such-cred", "y", "凭据不存在"),
    ]
    for did, cid, sec, label in probes:
        try:
            svc.authenticate(did, cid, sec)
        except GatewayError as exc:
            print(f"[{label}] 拒绝 device={did} reason={exc.code}")

    old = creds["dev-A"]
    rotated = svc.rotate_credentials("dev-A")
    print(f"设备 dev-A 轮换凭据 -> 新凭据 {rotated['credential_id']}")
    try:
        svc.authenticate("dev-A", old["credential_id"], old["secret"])
    except GatewayError as exc:
        print(f"[轮换后旧凭据] 拒绝 device=dev-A reason={exc.code}")

    _hr("3. 遥测：乱序/重复/回跳/配额")
    c = rotated
    ordered = [
        (1, 100.0, {"temp": 20.0}),
        (3, 120.0, {"temp": 22.0}),
        (2, 110.0, {"temp": 21.0}),       # 乱序
        (1, 100.0, {"temp": 20.0}),       # 重复
        (4, 90.0, {"temp": 19.0}),        # 时间戳回跳
        (5, 130.0, {"temp": 23.0}),
        (6, 140.0, {"temp": 24.0}),       # 超配额（quota=5）
    ]
    for seq, ts, metrics in ordered:
        try:
            r = svc.ingest_telemetry(
                "dev-A", c["credential_id"], c["secret"],
                {"seq": seq, "ts": ts, "metrics": metrics},
            )
            print(f"device=dev-A seq={seq} 判定={r['reason']} 依据={r['detail']}")
        except GatewayError as exc:
            print(f"device=dev-A seq={seq} 判定={exc.code} 依据={exc.message}")
    # 配额满后，旧 seq 重放仍按幂等处理（与配额拒绝可区分）
    replay = svc.ingest_telemetry(
        "dev-A", c["credential_id"], c["secret"],
        {"seq": 2, "ts": 110.0, "metrics": {"temp": 21.0}},
    )
    print(f"device=dev-A seq=2 配额满后重放 判定={replay['reason']}")
    print("最终统计:", json.dumps(svc.get_stats("dev-A")["totals"], ensure_ascii=False),
          "last_ts =", svc.get_stats("dev-A")["last_ts"])

    _hr("4. 固件分批灰度：批次1成功，批次2健康检查失败并回滚")
    svc.add_firmware("fw-1", "v2", 4096, "sha256:deadbeef")
    rollout = svc.create_rollout("fw-1", ["dev-A", "dev-B", "dev-C"], batch_size=2)
    b1, b2 = rollout["batches"]
    print("批次:", b1, b2)

    svc.start_batch(b1)
    for did in ("dev-A", "dev-B"):
        svc.report_device(b1, did, True, "刷机重启完成")
        svc.health_check(b1, did, True)
    print(f"批次1 状态={svc.get_batch(b1)['state']} "
          f"dev-A 版本={svc.device_view('dev-A')['current_version']}")

    svc.start_batch(b2)
    svc.report_device(b2, "dev-C", True, "刷机重启完成")
    failed = svc.health_check(b2, "dev-C", False)  # 触发整批回滚
    print(f"批次2 状态={failed['state']} 原因={failed['failure_reason']}")
    print(f"dev-C 版本保持={svc.device_view('dev-C')['current_version']} "
          f"(批次1不受影响: dev-A={svc.device_view('dev-A')['current_version']})")

    _hr("5. 重启恢复")
    svc2 = GatewayService(STATE_FILE, telemetry_quota=5)
    print("重启后批次1:", svc2.get_batch(b1)["state"],
          "| 批次2:", svc2.get_batch(b2)["state"],
          "| dev-A 版本:", svc2.device_view("dev-A")["current_version"])


if __name__ == "__main__":
    main()
