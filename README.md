# IoT 设备遥测接入与固件灰度升级网关（本地模拟）

纯本地实现的设备接入服务：设备注册与凭据鉴权、遥测幂等接收、固件分批灰度升级与回滚。
不依赖任何外部服务（无真实 MQTT / OTA 服务器 / 数据库），设备、网关与固件镜像均在进程内模拟，
状态通过 JSON 快照持久化。HTTP、CLI 模拟器、Python 门面三个入口共用同一套判定逻辑，
因此各入口给出的结论与原因码完全一致。

## 运行环境与依赖

- Python >= 3.14，依赖管理沿用仓库的 [uv](https://docs.astral.sh/uv/)；
- 运行依赖仅 `fastapi[standard]`；测试额外使用 `pytest`（已在 dev 依赖中）。

```bash
uv sync                       # 安装依赖
uv run pytest -q              # 运行全部单测
uv run python -m app.simulator --store demo-state.json   # 本地端到端演示
uv run uvicorn main:app --port 8000                      # 启动 HTTP 服务
IOT_GATEWAY_STORE=state.json uv run uvicorn main:app     # 带持久化快照启动
```

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `app/models.py` | 枚举原因码与领域模型（含 JSON 序列化） |
| `app/errors.py` | 业务异常，携带可区分的 `reason` 与 HTTP 状态码 |
| `app/store.py` | JSON 快照仓库（原子写 + 进程内 RLock），负责重启一致性 |
| `app/auth.py` | 设备注册、凭据签发/轮换/吊销、接入鉴权 |
| `app/telemetry.py` | 遥测校验、幂等去重、乱序/回跳处理、固定窗口配额 |
| `app/upgrade.py` | 固件发布、灰度活动、批次状态机、整批回滚 |
| `app/service.py` | 门面 `Gateway`，聚合三个服务，所有入口共用 |
| `app/api.py` | FastAPI 路由与 `create_app` 工厂 |
| `app/simulator.py` | 本地 CLI 模拟器，串起全部场景 |
| `main.py` | uvicorn 服务入口 |

## 1. 接入鉴权规则

设备先注册，再为其签发凭据。凭据为 `Bearer base64(credential_id:secret)`，
服务端只保存 `secret` 的 SHA-256。

- **轮换**：为同一设备再次签发凭据时，旧的活动凭据立即置为 `expired` 并立刻失效；
- **吊销**：吊销后凭据立即变为 `revoked` 并从设备当前凭据上摘除，旧令牌立即失效；
- **过期**：超过 `expires_at` 的凭据在下次鉴权时惰性置为 `expired`。

鉴权判定按顺序给出互相**可区分**的原因码（`AuthReason`）：

| reason | 含义 | HTTP |
| --- | --- | --- |
| `device_not_registered` | 设备未注册 | 404 |
| `credential_not_found` | 令牌无法解析/凭据不存在/凭据属于其他设备/未带令牌 | 401 |
| `credential_revoked` | 凭据已吊销（立即生效） | 401 |
| `credential_expired` | 凭据过期或已被轮换 | 401 |
| `secret_mismatch` | 凭据 ID 有效但密钥不符 | 401 |
| `ok` | 鉴权通过 | 200 |

## 2. 遥测幂等语义

上报体：`{"seq": <非负整数>, "ts": <正数时间戳>, "metrics": {字符串: 有限数值}}`。

处理顺序：

1. **格式校验** —— 非法直接拒绝 `invalid_payload`（422），不写入、不计数；
2. **幂等判重** —— 以 `(device_id, seq)` 为去重键。seq 已见过则返回
   `duplicate_replay`（200），不重复计数；即使窗口配额已满，重放仍判为重放，
   与「配额超限拒绝」明确区分；
3. **配额** —— 固定时间窗口（默认 60s、每窗 100 条，可在构造 `Gateway` 时配置），
   超限拒绝 `quota_exceeded`（429），计入 `rejected_count`；
4. **受理** —— `accepted`。

关于乱序与时间戳回跳：

- **乱序**：seq 小于历史最大值但从未见过（迟到包），照常受理，不会丢数据；
- **时间戳回跳**：ts 小于历史最大值也照常受理；`max_ts` 取历史最大值，不被回跳覆盖；
- 统计口径：`accepted_count`（唯一 seq 数）、`duplicate_count`（重放次数）、
  `rejected_count`（仅配额拒绝；格式非法不计数）、`max_seq`、`max_ts`。

遥测判定都会写日志，包含**设备标识、上报序号与判定依据**，例如：

```
iot.telemetry telemetry decision device_id=sensor-a seq=2 basis=accepted+out_of_order+ts_regression accepted_count=3 max_seq=3 max_ts=300.0
iot.telemetry telemetry decision device_id=sensor-a seq=1 basis=duplicate_replay accepted_count=3 duplicate_count=1
iot.telemetry telemetry decision device_id=sensor-a seq=bad basis=invalid_payload detail=seq 必须是非负整数
```

## 3. 升级批次状态机

固件镜像为本地模拟（版本号 + 内容派生的 checksum）。创建灰度活动时按 `batch_size`
把设备确定性地切成多个批次。

```
PENDING ──upgrade_batch(开始)──▶ IN_PROGRESS ──全部设备健康──▶ SUCCEEDED
                                   │  ▲
                  health 失败/设备异常/人工 rollback      │ report_device 上报中
                                   └──▶ ROLLED_BACK（终止态）
```

推进方式（两阶段，支持设备逐个上报）：

- `upgrade_batch(cid, bid, health_results=None)`：批次进入 `IN_PROGRESS` 并立即落盘，
  等待设备上报；
- `report_device(...)`：批次进行中设备上报结果；任一设备 `ok=False` 立即
  **整批回滚**；全部上报健康则批次成功；
- `upgrade_batch(..., health_results={...})`：传入结果字典时一次性终结批次
  （未列出的设备视为健康，传 `{}` 表示全健康）；
- `rollback_batch(...)`：人工触发进行中批次整体回滚。

关键一致性规则：

- **严格按批次顺序**：前序批次未 `SUCCEEDED`，后续批次以
  `previous_batch_not_succeeded`（409）拒绝，不能跳批；
- **失败隔离**：批次内任一设备异常/健康检查失败，整批每台设备都恢复到各自升级前版本；
  失败设备 `failed`、被连带回滚设备 `rolled_back`，状态可区分；
  已成功的其他批次与设备版本不受影响；
- **版本不被中途覆盖**：设备 `firmware_version` 只在批次整体成功时推进，
  回滚恢复 `from_version`；
- **并发安全**：检查、变更、快照写入在同一把 `Store` RLock 内完成。并发升级请求中
  恰好一个成功，其余以 `batch_in_progress` / `batch_already_succeeded` /
  `batch_rolled_back`（均 409）拒绝，进度与设备版本互不覆盖；
- **重启一致**：进入 `IN_PROGRESS`、成功、回滚后都会立刻把快照原子写入 JSON
  （临时文件 + `os.replace`），新进程从快照恢复后状态机与设备版本完全一致。

## 4. HTTP 接口（前缀 `/api/v1`）

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /devices/{id}/register` | 注册设备（重复注册 409 `device_already_registered`） |
| `POST /devices/{id}/credentials` | 签发/轮换凭据，返回明文 `secret` 与 `token` |
| `POST /devices/{id}/credentials/{cid}/revoke` | 吊销凭据 |
| `POST /devices/{id}/telemetry` | 上报遥测（需 `Authorization: Bearer`） |
| `GET  /devices/{id}/telemetry` | 查询遥测统计（需鉴权） |
| `POST /firmware` | 发布本地固件镜像 `{"version":"2.0.0"}` |
| `POST /campaigns` | 创建灰度活动 `{"firmware_version","device_ids","batch_size"}` |
| `GET  /campaigns/{cid}` | 查询批次进度与各设备升级状态 |
| `POST /campaigns/{cid}/batches/{bid}/upgrade` | 开始/推进批次 |
| `POST /campaigns/{cid}/batches/{bid}/report` | 批次内设备上报 |
| `POST /campaigns/{cid}/batches/{bid}/rollback` | 整批回滚 |

成功响应为 `{"ok": true, ...}`；拒绝为 `{"ok": false, "reason": <原因码>, "message": ...}`。
遥测的 `accepted` 与 `duplicate_replay` 都是 200（幂等重放不是错误），
`invalid_payload` 为 422，`quota_exceeded` 为 429，可据 `reason` 明确区分。

## 5. 本地验证方法

```bash
# 1) 单元测试（覆盖重复/乱序遥测、时间戳回跳、凭据吊销/轮换/过期、
#    批次失败整批回滚、并发升级、重启一致性、HTTP 各入口）
uv run pytest -q

# 只看遥测测试的判定日志（设备标识/序号/依据）
uv run pytest tests/test_telemetry.py -o log_cli=true -o log_cli_level=INFO -q

# 2) 端到端模拟器：打印每个步骤的 ok/reason，结尾附带重启后进度
uv run python -m app.simulator --store demo-state.json

# 3) HTTP 手工验证
uv run uvicorn main:app --port 8000
# 注册 -> 取 token -> 带 token 上报遥测（示例）
curl -s -X POST localhost:8000/api/v1/devices/d1/register
TOKEN=$(curl -s -X POST localhost:8000/api/v1/devices/d1/credentials | python -c "import json,sys;print(json.load(sys.stdin)['token'])")
curl -s -X POST localhost:8000/api/v1/devices/d1/telemetry \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"seq":1,"ts":1700000000,"metrics":{"temp":36.5}}'
```

### 测试与场景对照

| 场景 | 测试用例 |
| --- | --- |
| 重复 / 乱序 / 时间戳回跳遥测 | `tests/test_telemetry.py` |
| 非法格式与配额拒绝、重放与超限区分 | `tests/test_telemetry.py` |
| 未注册 / 凭据不存在 / 过期 / 吊销 / 密钥不符 | `tests/test_auth.py` |
| 轮换后旧凭据立即失效 | `tests/test_auth.py::test_rotation_invalidates_old_credential` |
| 批次失败整体回滚、不影响其他批次 | `tests/test_upgrade.py::test_batch_failure_rolls_back_only_that_batch` |
| 设备上报异常触发回滚 | `tests/test_upgrade.py::test_device_report_triggers_rollback` |
| 并发升级进度/版本不互相覆盖 | `tests/test_upgrade.py::test_concurrent_upgrade_does_not_corrupt` |
| 重启后状态一致 | `tests/test_upgrade.py::test_state_survives_restart` |
| HTTP 入口结论与门面一致 | `tests/test_api.py` |
