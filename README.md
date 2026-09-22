# IoT 设备遥测接入与固件灰度升级网关（本地模拟）

纯本地模拟的设备遥测网关：设备注册/接入鉴权、遥测幂等上报、固件分批灰度升级与回滚。
不依赖任何外部服务（无数据库 / MQTT / 对象存储），状态持久化到本地 JSON 文件。

- 服务入口沿用工程骨架：`main.py` 暴露 FastAPI `app`（`uvicorn main:app`）。
- 依赖管理沿用 `uv`（见 `pyproject.toml`），仅使用仓库已有依赖 FastAPI
  及其自带的 `httpx`；测试只用 Python 标准库 `unittest`，无需额外安装。

## 目录结构

```
main.py                  # FastAPI 入口（沿用骨架）
gateway/
  errors.py              # 稳定错误码（可区分的拒绝原因）与 GatewayError
  models.py              # 设备/凭据/遥测/固件/批次领域模型与状态枚举
  credentials.py         # 凭据签发、轮换、吊销、鉴权
  devices.py             # 设备注册表
  firmware.py            # 本地固件镜像（元数据模拟）
  telemetry.py           # 遥测格式校验与基于 seq 的幂等处理
  upgrades.py            # 分批灰度升级状态机与整批回滚
  store.py               # JSON 原子持久化 + 进程内 RLock
  service.py             # 应用服务：加锁、持久化、装配（HTTP/CLI 共用）
  api.py                 # FastAPI 路由与错误码 -> HTTP 状态映射
scripts/demo.py          # CLI 端到端演示（与 HTTP 同一套 service 结论）
tests/                   # unittest 测试（日志含设备标识、上报序号、判定依据）
```

## 1. 接入鉴权规则

- 设备注册（`POST /devices`）后获得第一把接入凭据：`credential_id` + `secret`。
- 遥测请求通过请求头携带凭据：
  `X-Credential-Id`、`X-Credential-Secret`。
- 鉴权按固定顺序判定，拒绝原因通过**错误码**区分（HTTP 同时给出状态码）：

| 错误码 | 含义 | HTTP |
| --- | --- | --- |
| `DEVICE_NOT_FOUND` | 设备未注册 | 404 |
| `CREDENTIAL_MISSING` | 请求未携带凭据 | 401 |
| `CREDENTIAL_NOT_FOUND` | 凭据不存在（已轮换）或密钥不匹配 | 401 |
| `CREDENTIAL_REVOKED` | 凭据已被吊销 | 401 |
| `CREDENTIAL_EXPIRED` | 凭据已过 `expires_at` | 401 |

- **轮换**（`POST /devices/{id}/credentials/rotate`）：签发新凭据的同时立即吊销该设备
  全部旧凭据，旧凭据在轮换返回后**下一次请求即失效**。
- **吊销**（`POST /devices/{id}/credentials/{cid}/revoke`）：单把凭据立即失效。
- 凭据状态随 JSON 落盘，重启后吊销/过期结论保持一致。

## 2. 遥测上报与幂等语义

上报体：`{"seq": <非负整数>, "ts": <数值时间戳>, "metrics": {"k": <number>, ...}}`

- `seq` 为设备内单调递增序号，是唯一去重键。
- **乱序**：seq 3 先于 seq 1 到达，两者都正常受理，`last_seq` 取最大 seq。
- **重复 / 重放**：相同 seq 再次上报返回 `reason=DUPLICATE`，
  计数、指标累加、`last_ts` 全部不变（即使携带不同内容也不覆盖）。
- **时间戳回跳**：更小的 `ts` 不更新 `last_ts`，但数据照常受理，统计不丢。
- **非法格式**：抛 `TELEMETRY_BAD_FORMAT`（HTTP 400），与幂等重放明确区分。
- **配额**：每设备受理（非重复）条数达到配额后，新 seq 被拒，
  `TELEMETRY_QUOTA_EXCEEDED`（HTTP 429）；**配额满后旧 seq 重放仍按 DUPLICATE 成功**，
  因此“超配额拒绝”与“幂等重放”在任何入口下都可区分。

设备统计：`received`（首次受理）、`duplicates`（重放）、`rejected`（拒绝）、
`last_seq`、`last_ts`、`totals`（指标按 seq 只累加一次）。

## 3. 固件分批灰度升级状态机

- 固件：`POST /firmware`（`firmware_id/version/size/checksum`，纯本地元数据）。
- 创建灰度任务：`POST /rollouts`
  `{firmware_id, device_ids, batch_size}`，设备按 `batch_size` 自动切分多个批次，
  创建即占用设备（`active_batch_id`）。
- 设备单批次内流程：
  `PENDING → FLASHING → REBOOTING → HEALTHY`，
  任一步失败：`→ FAILED`，并触发整批回滚。

批次状态机：

```
PENDING ──start──▶ IN_PROGRESS ──全部设备 HEALTHY──▶ SUCCEEDED
   │                   │
   │                   └──任一设备上报失败/健康检查失败──▶ ROLLING_BACK ──▶ ROLLED_BACK
   └──cancel（未开始）──────────────────────────────▶ CANCELLED
```

关键一致性保证：

- **版本单一提交点**：设备 `current_version` 只在整批 `SUCCEEDED` 时提交一次；
  失败/回滚路径不改版本，并显式恢复为 `from_version`（双保险）。
- **整批回滚且批次隔离**：回滚只处理本批次设备并释放占用；
  其他已成功/进行中的批次版本与进度不受影响。
- **并发互斥**：设备被批次占用期间，第二个升级请求得到
  `DEVICE_BUSY`（HTTP 409），进度与当前版本不会互相覆盖。
- **重复上报幂等**：对同一设备重复 `report` / `health` 不改变既有进度。
- **重启一致**：每次状态迁移在进程级 `RLock` 内完成，并以
  “临时文件 + `os.replace`”原子写盘；重启后批次状态、设备版本、占用关系保持一致。
- 状态文件路径由环境变量 `GATEWAY_STATE_FILE` 指定；不指定则为纯内存模式。

## 4. HTTP 接口速览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /devices` | 注册设备并返回首把凭据 |
| `GET /devices/{id}` | 设备信息（版本、凭据、占用批次） |
| `POST /devices/{id}/credentials` | 追加签发凭据 |
| `POST /devices/{id}/credentials/rotate` | 轮换（旧凭据立即失效） |
| `POST /devices/{id}/credentials/{cid}/revoke` | 吊销凭据 |
| `POST /devices/{id}/telemetry` | 遥测上报（凭据走请求头） |
| `GET /devices/{id}/stats` | 遥测统计 |
| `POST /firmware` | 登记本地固件镜像 |
| `POST /rollouts` | 创建分批灰度任务 |
| `GET /batches` / `GET /batches/{id}` | 批次查询 |
| `POST /batches/{id}/start` | 开始批次 |
| `POST /batches/{id}/report` | 设备上报 `{device_id, success, message}` |
| `POST /batches/{id}/health` | 健康检查 `{device_id, healthy}` |
| `POST /batches/{id}/rollback` | 人工整批回滚 |

错误响应统一为：`{"error": "<错误码>", "message": "<可解释原因>", "device_id": "..."}`。

## 5. 本地验证

```bash
# 环境：uv + Python 3.14（沿用工程配置），首次会自动同步 fastapi[standard]

# (1) 全部单测（标准库 unittest，无需额外依赖）
uv run python -m unittest discover -s tests -v

# (2) CLI 端到端演示：注册/鉴权拒绝/乱序重复遥测/批次成功与失败回滚/重启恢复
uv run python scripts/demo.py

# (3) HTTP 服务
uv run uvicorn main:app --reload
#   curl -s -X POST localhost:8000/devices -H 'Content-Type: application/json' \
#        -d '{"device_id":"dev-1"}'
```

测试日志按要求打印 **设备标识（device=...）、上报序号（seq=...）与判定依据
（reason / 错误码 + 文字说明）**，例如：

```
INFO device=dev-tel seq=2 ts=80.0 判定=ACCEPTED 依据=seq=2 首次受理...（设备时间戳回跳，last_ts 不更新）
INFO device=dev-tel seq=2 ts=20.0 判定=DUPLICATE 依据=seq=2 已受理过，按幂等重放处理，统计不变
INFO device=d3 seq=- 判定依据=健康检查失败；其他批次 d1/d2 仍为 v9
```

## 6. 主要拒绝/判定码汇总

`DEVICE_EXISTS`、`DEVICE_NOT_FOUND`、`CREDENTIAL_MISSING`、
`CREDENTIAL_NOT_FOUND`、`CREDENTIAL_REVOKED`、`CREDENTIAL_EXPIRED`、
`TELEMETRY_BAD_FORMAT`、`TELEMETRY_QUOTA_EXCEEDED`、`FIRMWARE_NOT_FOUND`、
`FIRMWARE_EXISTS`、`BATCH_NOT_FOUND`、`BATCH_NOT_ACTIVE`、
`BATCH_DEVICE_NOT_IN`、`DEVICE_BUSY`、`ROLLOUT_NOT_FOUND`。
