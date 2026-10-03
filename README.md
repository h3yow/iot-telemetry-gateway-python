# IoT 设备遥测接入与固件灰度升级网关（本地模拟）

纯本地实现的设备接入服务：设备注册与凭据鉴权、遥测幂等接收、固件分批灰度升级与回滚，
以及多活动并发下的**设备占用互斥、活动暂停/继续/取消、每设备升级往来记录**。
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
| `app/upgrade.py` | 固件发布、灰度活动、批次状态机、整批回滚；**设备占用治理、活动暂停/继续/取消、设备升级往来** |
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

## 4. 设备占用治理与活动生命周期

在既有批次状态机之上增加的治理层，**只新增结论，不改写已有批次/拒绝语义**。

### 4.1 设备占用（跨活动互斥）

- 一台设备在任意时刻最多被一个活动的**进行中批次**占用；占用在批次开始
  （`IN_PROGRESS`）时获取，随快照落盘；
- 即使多个活动请求同时打进来、目标版本还不一样，「检查占用 + 写入占用 + 快照」
  在同一把 `Store` RLock 内原子完成：恰好一个成功，其余**立即**被拒，原因码
  `device_occupied_by_other_campaign`（409），独立于既有的
  `batch_in_progress` / `device_upgrading` 等原因；
- 被拒一方不改动任何设备版本/状态，也不影响已推进的一方（all-or-nothing，
  批次内任一设备被占则整批不开始）；
- **自动释放**：批次走到 `SUCCEEDED` / `ROLLED_BACK`，或活动取消引发进行中
  批次回滚后，占用立即释放；之后设备可正常参加新活动；
- **重启 / 版本升级恢复**：占用关系持久化在快照的 `device_occupations` 中；
  此外每次加载快照时都会以「进行中批次」为准**重建占用**——
  - 从上一版原地升级、沿用没有 `device_occupations` 字段的旧快照时，
    仍有进行中批次（设备停在 `upgrading`）的占用会被推导恢复：
    只要批次没走到成功/回滚/取消这些终态，它名下的设备就仍被占着，
    占用查询能查到活动与批次，跨活动争抢仍按
    `device_occupied_by_other_campaign` 拒绝；
  - 快照里残留指向已终态/不存在批次的占用会被清掉，不会卡住不释放；
  - 重建是幂等的：反复重启结果一致，不会重复占上、也不会凭空消失；
- 查询：`GET /api/v1/devices/{id}/occupation` 返回当前占用它的活动与批次，
  未被占用时 `occupied: false`。

### 4.2 活动生命周期：暂停 / 继续 / 取消

活动有三种治理状态：`active`（默认）/ `paused` / `cancelled`；批次新增终态
`cancelled`。既有批次状态机与原因码全部保留。

- `POST /campaigns/{cid}/pause`：暂停后**不能再开始新批次**（尝试推进待开始
  批次返回 `campaign_paused`，409）；已经 `IN_PROGRESS` 的批次不受影响，
  设备仍可继续上报；重复暂停幂等；
- `POST /campaigns/{cid}/resume`：继续后**从原来的断点接着走**（顺序规则、
  进行中批次的上报进度都原样保留）；对活动中的活动调用幂等；已取消的活动
  不能继续（`campaign_cancelled`）；
- `POST /campaigns/{cid}/cancel`：只处理仍在进行中的部分——
  - `IN_PROGRESS` 批次：走既有整批回滚路径，每台设备恢复各自升级前版本
    （回滚只发生一次），并释放占用；
  - `PENDING` 批次：标记为 `cancelled`，不占用、不改设备；
  - 已 `SUCCEEDED` / `ROLLED_BACK` 的批次：原样保留，绝不二次回滚或改版本；
  - 重复取消幂等：不追加记录、不重复回滚；取消后不能暂停/继续/推进。

### 4.3 设备升级往来记录

每台设备每次升级往来按**实际发生顺序**追加一条事件（快照 `upgrade_events`，
重启后仍在），通过 `GET /api/v1/devices/{id}/upgrade-history` 查询：

| event_type | 时机 |
| --- | --- |
| `upgrade_started` | 设备随批次进入升级（记录 `from_version → to_version`） |
| `upgrade_succeeded` | 批次整体成功，设备版本推进到目标版本 |
| `upgrade_rolled_back` | 健康失败/人工回滚/活动取消导致整批回滚 |
| `upgrade_cancelled` | 设备随待开始批次被活动取消（未实际升级） |

每条事件的 `from_version` / `to_version` 表示**这次往来里设备真实的版本
变化**：回滚事件的落点（`to_version`）是设备退回后的原版本，而不是当初要
升上去的目标版本。因此把一台设备的往来按 `seq` 顺序连起来，每条的
`to_version` 都衔接下一条的 `from_version`，最后一条的 `to_version`
就是设备当前所在版本——重启后依然成立。

> 兼容范围：`upgrade_cancelled` 当前仅记录随待开始批次取消的设备（这些设备
> 从未被占用、版本从未变化）；进行中批次被取消时走回滚路径，记为
> `upgrade_rolled_back`。记录只追加、顺序号 `seq` 每台设备内单调递增。
> 旧版本快照里没有 `upgrade_events` 字段时按空历史处理，不影响占用恢复。

### 4.4 新增拒绝原因码（均 409，彼此可区分）

| reason | 含义 |
| --- | --- |
| `device_occupied_by_other_campaign` | 设备正被另一个进行中的活动占用 |
| `campaign_paused` | 活动已暂停，不能推进新批次 |
| `campaign_cancelled` | 活动已取消，不能继续/推进 |
| `batch_cancelled` | 批次已随活动取消（在已取消批次上推进时） |

## 5. HTTP 接口（前缀 `/api/v1`）

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
| `POST /campaigns/{cid}/pause` | 暂停活动（幂等） |
| `POST /campaigns/{cid}/resume` | 继续活动，从断点接着走（幂等） |
| `POST /campaigns/{cid}/cancel` | 取消活动：回滚进行中批次、取消待开始批次、不动已终态批次（幂等） |
| `GET  /devices/{id}/occupation` | 查询设备当前被哪个活动/批次占用 |
| `GET  /devices/{id}/upgrade-history` | 查询设备历次升级往来（按发生顺序） |

成功响应为 `{"ok": true, ...}`；拒绝为 `{"ok": false, "reason": <原因码>, "message": ...}`。
遥测的 `accepted` 与 `duplicate_replay` 都是 200（幂等重放不是错误），
`invalid_payload` 为 422，`quota_exceeded` 为 429，可据 `reason` 明确区分。

## 6. 本地验证方法

```bash
# 1) 单元测试（覆盖重复/乱序遥测、时间戳回跳、凭据吊销/轮换/过期、
#    批次失败整批回滚、并发升级、重启一致性、HTTP 各入口，
#    以及跨活动真实并发争抢、占用释放、暂停/继续/取消、幂等、重启恢复）
uv run pytest -q

# 压一压并发争抢（屏障同步的两个活动同时开跑），多跑几轮
uv run pytest tests/test_governance.py -q

# 重启/原地升级恢复场景（旧快照占用重建、回滚落点、反复重启幂等），
# 用例日志会打印覆盖的业务场景与关键断言点
uv run pytest tests/test_recovery.py -o log_cli=true -o log_cli_level=INFO -q

# 只看遥测测试的判定日志（设备标识/序号/依据）
uv run pytest tests/test_telemetry.py -o log_cli=true -o log_cli_level=INFO -q

# 2) 端到端模拟器：打印每个步骤的 ok/reason，覆盖占用争抢、
#    暂停/继续、取消（进行中回滚 + 待开始取消 + 已成功保留），
#    结尾附带重启后的占用与往来记录
uv run python -m app.simulator --store demo-state.json

# 3) HTTP 手工验证
uv run uvicorn main:app --port 8000
# 注册 -> 取 token -> 带 token 上报遥测（示例）
curl -s -X POST localhost:8000/api/v1/devices/d1/register
TOKEN=$(curl -s -X POST localhost:8000/api/v1/devices/d1/credentials | python -c "import json,sys;print(json.load(sys.stdin)['token'])")
curl -s -X POST localhost:8000/api/v1/devices/d1/telemetry \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"seq":1,"ts":1700000000,"metrics":{"temp":36.5}}'

# 4) 占用与生命周期手工验证（先建好两个共享设备的活动）
curl -s -X POST localhost:8000/api/v1/campaigns/cid/pause -d '{}' -H 'Content-Type: application/json'
curl -s -X POST localhost:8000/api/v1/campaigns/cid/resume -d '{}' -H 'Content-Type: application/json'
curl -s -X POST localhost:8000/api/v1/campaigns/cid/cancel \
  -d '{"message":"人工取消"}' -H 'Content-Type: application/json'
curl -s localhost:8000/api/v1/devices/d1/occupation
curl -s localhost:8000/api/v1/devices/d1/upgrade-history
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
| 真实并发跨活动争抢设备，恰一成功、独立原因码、输家不动设备 | `tests/test_governance.py::test_concurrent_cross_campaign_contention_only_one_wins` |
| 被拒一方不改动批次内任何设备 | `tests/test_governance.py::test_rejected_campaign_does_not_touch_other_devices` |
| 成功/回滚终态后占用自动释放、可参加新活动 | `tests/test_governance.py::test_occupation_released_after_terminal_states` |
| 暂停挡新批次、继续从断点推进、重复操作幂等 | `tests/test_governance.py::test_pause_blocks_new_batches_resume_continues` |
| 取消只回滚进行中、取消待开始、保留已成功 | `tests/test_governance.py::test_cancel_rolls_back_in_progress_and_keeps_succeeded` / `::test_cancel_pending_batches_marked_cancelled` |
| 重复取消不二次回滚、不追加记录、不改版本 | `tests/test_governance.py::test_cancel_is_idempotent_no_double_rollback` |
| 往来记录顺序与实际一致、跨活动连续可查 | `tests/test_governance.py::test_device_history_order_matches_occurrence` |
| 占用/活动状态/版本/记录重启一致，重启后仍互斥 | `tests/test_governance.py::test_occupation_and_history_survive_restart` |
| 沿用上一版快照原地升级：占用恢复、争抢拒绝、原批次继续推进 | `tests/test_recovery.py::test_legacy_snapshot_in_progress_batch_still_occupies` |
| 回滚往来版本落点与设备真实版本一致，重启后可一路追到当前版本 | `tests/test_recovery.py::test_rollback_history_versions_match_real_landing` |
| 反复重启占用不丢不重，终态释放与取消幂等 | `tests/test_recovery.py::test_repeated_restarts_keep_occupation_and_release_idempotent` |
| 残留指向终态批次的占用重启后被清掉 | `tests/test_recovery.py::test_stale_occupation_of_terminal_batch_is_dropped` |
| HTTP 跨活动并发争抢返回可区分原因码 | `tests/test_governance.py::test_concurrent_http_contention_distinguishable_reason` |
| HTTP 入口结论与门面一致 | `tests/test_api.py` |
