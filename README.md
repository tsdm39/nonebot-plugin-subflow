# nonebot-plugin-subflow

字幕组任务管理 Bot — 对接腾讯文档智能表，在 QQ 群里管理"接活 / 完成 / 依赖跟踪 / 自动通知"。

> 设计文档：[md/字幕组Bot设计文档.md](md/字幕组Bot设计文档.md)
> 实现方案与决策记录：[md/Bot实现方案.md](md/Bot实现方案.md)

---

## 它能做什么

字幕组日常按"集"推进，每集要走多道工序（翻译 / 时轴 / 校对 / 后期 / 监制 / 压制 等），原本靠手动在腾讯文档智能表里改进度，常漏接、重复接、信息滞后。本 Bot 把这套流程搬进 QQ 群：

- **接活**：群员发 `/接活 淡岛百景 7 翻译 1` → Bot 自动把进度从「未分配」改成「已分配」、写入 QQ 号到「组员」列。**接活可提前**，前置工序没完成也能先认领排队。写库前会先重读该行最新状态，**不会覆盖**别人刚手动填的认领（D19）
- **完成 + 依赖通知**：`/完成` 时 Bot 校验前置工序是否完成（没完成则拒绝完成），完成后自动判断下游工序前置是否全部满足，满足则在工作群通知「校对现在可以接了 → /接活 ...」
- **进度看板**：`/进度 淡岛百景 07` 一键查看本集所有工序的当前状态
- **可定制工序**：每个番剧的工序链通过 DSL 自定义（`/设置流水线 淡岛百景 翻译[分段] → 时轴[分段] → 校对 → 后期 → 监制 → 压制`）；同段串行（P1 翻译→P1 时轴），不同段并行
- **总群 / 工作群分流**：每番一个工作群、一个总群俯瞰所有番；总群只能查询、不接收推送
- **改流水线不影响已有集**：每集创建时快照一份当前流水线，后续改流水线只影响新建集
- **外部直改也有提醒**：有人绕过 Bot 直接在腾讯文档里改进度 / 填组员 / 标完成 / 增删行，定时同步时会 diff 出来并在工作群播报；手动标完成同样触发下游解锁提示（D17，可开关）

---

## 架构概览

```
QQ 群（工作群 / 总群）
   ↕ OneBot v11
NapCat（QQ 协议适配）
   ↕ WebSocket
NoneBot2 + nonebot-plugin-subflow
   ├─ commands.py     ← 19 个命令处理器
   ├─ task_manager.py ← 业务核心（接活/完成/依赖/确认）
   ├─ cache.py        ← 内存缓存 + 30 分钟同步 + per-record 锁
   ├─ bindings.py     ← 群-番剧绑定
   ├─ pipeline.py     ← 工序链 + per-集快照
   └─ storage/tencent_doc.py
   ↕ HTTPS
腾讯文档开放平台 API（智能表 CRUD）
```

分层严格解耦，未来换飞书表格只需替换 `storage/*.py`；换 AstrBot 等框架只需替换 `commands.py`。

---

## 快速开始（Docker）

### 0. 前置

- Docker / Docker Compose
- 腾讯文档开放平台已通过审核的应用，能在后台拿到：
  - `client_id`
  - `open_id`
  - `access_token`（30 天 JWT，到期前手动续）
- 一个 QQ 号给 Bot 用，加进你要管理的工作群和总群
- 一个 OneBot v11 实现（推荐 [NapCat](https://github.com/NapNeko/NapCatQQ)）

### 1. 准备数据

复制环境变量模板：

```bash
cp .env.example .env
cp docker-compose.yml.example docker-compose.yml
```

编辑 `.env`：至少填 `TENCENT_DOC_CLIENT_ID` / `TENCENT_DOC_OPEN_ID` / `TENCENT_DOC_ACCESS_TOKEN`（单 key），或改填 `SUBFLOW_TENCENT_DOC_KEYS` 多 key 数组（见下方「日常运维 · 提升日额度」）；其他按注释提示按需填。
（`.env` 和 `docker-compose.yml` 都被 gitignore，本地版本随便改；upstream 模板只跟 `.example` 走。）

### 2. 在腾讯文档里准备智能表

每个番剧建一个子表，**列结构必须包含**：

| 列名 | 类型 | 备注 |
|---|---|---|
| 类型 | 单选 | 选项至少含：翻译/时轴/校对/后期/监制/压制 |
| 集数 | 文本 | Bot 会按归一化匹配（`07` `7` `第7集` 等价） |
| 分段 | 文本 | 纯数字串：分段工序填 `1` `2` `3`；不分段的工序填 `0` |
| 组员 | 文本 | Bot 写 QQ 号；人手填可以是昵称 |
| 进度 | 单选 | 必含：未分配/已分配/进行中/已完成/归档 |
| 完成时间 | 日期 / 日期时间 | Bot 在 `/完成` 时自动写 |
| 备注 | 文本 | 自由备注 |

> 表里多出来的列（如「项目」「开始时间」「相关流程」）Bot 不读不写，留给人手填。

### 3. 启动 Bot

**方案 A — 已有外部 NapCat**：

```bash
docker compose up -d
```

然后在你的 NapCat 配置里指向 `ws://你的-nonebot-host:8080/onebot/v11/ws`。

**方案 B — 容器化 NapCat 一锅端**：

打开 `docker-compose.yml`，取消 `napcat` 服务块和 `depends_on` 那 4 行的注释，再：

```bash
docker compose up -d
```

打开 `http://localhost:6099` 进入 NapCat WebUI 扫码登录。

### 4. 在 QQ 群里配置

进工作群发：

```
/绑定id 300000000$RdEfaprsBpFo ss_3k813b 淡岛百景
```

`300000000$RdEfaprsBpFo` 是真 fileID；如果你只有腾讯文档 URL 里的分享 ID（如 `DUmRFZmFwcnNCcEZv`），Bot 也接受 —— 会自动通过腾讯的 converter 接口换成真 fileID。`ss_3k813b` 是子表 ID，从 URL 的 `?tab=ss_xxx` 部分取。

绑定后即可正常使用所有命令。

---

## 本地部署（pip / uv）

不想用 Docker、想直接在主机/服务器上跑时用这个。统一入口是 `python bot.py`，pip 和 uv 两条路只差"建环境 + 装依赖"那一步，其余完全一样。

### 前置

- Python ≥ 3.10
- 一个 OneBot v11 实现（推荐 [NapCat](https://github.com/NapNeko/NapCatQQ)），与 Bot 网络可达
- 腾讯文档凭据：单 key 填 `TENCENT_DOC_CLIENT_ID/OPEN_ID/ACCESS_TOKEN`，或多 key 填 `SUBFLOW_TENCENT_DOC_KEYS`（见上方配置项）

### 1. 拉代码 + 配置

```bash
git clone <仓库地址> nonebot-plugin-subflow
cd nonebot-plugin-subflow
cp .env.example .env        # Windows: copy .env.example .env
```

编辑 `.env` 填好凭据等（至少填一套腾讯凭据；NapCat 连接见第 3 步）。

### 2. 装依赖并启动（pip 或 uv，二选一）

**pip：**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install .
python bot.py
```

**uv：**

```bash
# 装 uv 见 https://docs.astral.sh/uv/
uv run python bot.py               # 自动选 Python、建 .venv、装依赖、启动
# 或先预装再跑： uv sync && uv run python bot.py
```

> `uv run` / `uv sync` 会在仓库里生成 `uv.lock` 锁定依赖；不想入库可把它加进 `.gitignore`。

### 3. 让 NapCat 连上来

在 NapCat 里配置**反向 WebSocket**指向 `ws://<Bot主机>:8080/onebot/v11/ws`（端口由 `.env` 的 `PORT` 决定，默认 8080）。

首次启动后会自动创建 `./data` 目录（存 `bindings.json` / `pipelines.json` / `episode_pipelines.json`）。然后在工作群发 `/绑定id ...` 即可开始使用（同上方 Docker 第 4 步）。

> 注意：要在**含 `.env` 的目录**（仓库根）下运行 —— NoneBot 从当前工作目录读取 `.env`。`python bot.py` 是前台运行，关掉终端进程即停（需要常驻请自行用 systemd / nohup / NSSM 等）。

---

## 命令速查

> 写命令仅在工作群可用（D9）；查询命令工作群+总群均可。
> 管理命令需要：群主 / 群管 / `SUBFLOW_ADMIN_QQ_LIST` 内的 QQ。

### 绑定与流水线

```
/绑定id <fileID或URL编码ID> <sheetID> <别名>    管理员，绑定子表
/解绑 <别名>                                    管理员
/绑定列表 [全部]                                  「全部」需超管
/设置流水线 <番剧名> <DSL>                       管理员，详见下方 DSL 语法
/查看流水线 <番剧名>
```

**流水线 DSL 示例**：

```
/设置流水线 淡岛百景 翻译[分段] → 时轴[分段] → 校对 → 后期 → 监制 → 压制
/设置流水线 短片 翻译 → 校对 → 压制
```

- `→` 串行，逗号分隔的工序并行
- `[分段]` 标记需按分段展开（新建集时按 1/2/3/... 各生成一条）；不带 `[分段]` 的工序生成 1 条「分段=0」记录
- **同段串行**（D13）：当上下游**都标 [分段]** 时，依赖按"同段"判断 — P1 翻译完成后才能 `/完成` P1 时轴，不影响 P2/P3；若有一方不分段（如下游 `校对`），仍按"所有上游段都完成"判断。注意依赖只在 `/完成` 时校验（D15），`/接活` 不受前置约束
- 容忍 `->` `=>`、全/半角逗号 `，`、全/半角方括号 `［分段］`

### 集级操作

```
/新建集 <番剧名> <集数> [分段数=1]                 管理员
/新建特殊 <番剧名> <集数标识> <类型1> [类型2] ...   管理员，OP/ED 用
/删除任务 <番剧名> <集数> [类型] [分段]            管理员，需「确认删除」二次确认
确认删除                                          回应上一条 /删除任务
/修改任务 <番剧名> <集数> <类型> [分段] <字段>=<值>  管理员
/归档 <番剧名> <集数>                              管理员
```

**示例**：

```
/新建集 淡岛百景 7 3              ← 翻译/时轴各 3 段（"1"/"2"/"3"），其它工序 1 条（"0"）
/新建集 淡岛百景 7                ← 等价 /新建集 淡岛百景 7 1
/新建特殊 淡岛百景 OP 翻译 时轴 校对 后期 压制   ← OP 集，所有工序「分段=0」
```

### 任务操作

```
/接活 <番剧名> <集数> <类型> [分段]
/完成 <番剧名> <集数> <类型> [分段]
/放弃 <番剧名> <集数> <类型> [分段]
/进行中 <番剧名> <集数> <类型> [分段]      已分配 → 进行中
```

`/完成` 后 Bot 会自动计算下游工序解锁情况并发通知。

> D15：`/接活` 可提前认领（不校验前置），但 `/完成` 会校验前置——前置工序没全部完成时拒绝完成并提示（`前置任务未完成，无法完成：['翻译 2']`）。
> D3：`/完成` 全员可执行，但发送者 ≠ 当前组员时会附提醒（`⚠️ 你不是该任务的当前组员（@原组员），已为你标记完成`）。`/放弃` 同。

### 查询（工作群 + 总群均可）

```
/进度 <番剧名> [集数]
/我的任务
/待接 [番剧名]
```

工作群单绑定时番剧名可省；多绑定时省略会让你选。

> `/待接` 列出全部「未分配」任务（D15：接活不校验前置，故不再按前置过滤）。
> `/进度` 进度看板、`/我的任务` 抬头、`/删除任务` 确认摘要里的组员都用真实 @ 艾特码（D16），群里能直接戳到人。

---

## 配置项（.env）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `TENCENT_DOC_CLIENT_ID` | △ | "" | 开放平台应用 client_id（单 key 写法；配了 `SUBFLOW_TENCENT_DOC_KEYS` 则可省） |
| `TENCENT_DOC_OPEN_ID` | △ | "" | open_id（同上） |
| `TENCENT_DOC_ACCESS_TOKEN` | △ | "" | 30 天 JWT（同上） |
| `SUBFLOW_TENCENT_DOC_KEYS` | △ | `[]` | **多 key 轮换池**（D18），JSON 数组 `[{client_id,open_id,access_token},...]`；非空时取代上面三元组 |
| `SUBFLOW_TENCENT_DOC_RATE_LIMIT_RETS` | | `[]` | 视为限流的 ret 码集合，命中则该 key 短时冷却+换把（腾讯未公布码，观测到后填） |
| `SUBFLOW_TENCENT_DOC_KEY_COOLDOWN` | | 60 | 某 key 触发限流后的冷却秒数 |
| `TENCENT_DOC_DEFAULT_FILE_ID` | △ | "" | 启用 `/绑定 <番剧名>`（按名查找子表）必填 |
| `SUBFLOW_MAIN_GROUP_ID` | △ | None | 总群 QQ 群号，留空则不启用总群 |
| `SUBFLOW_ADMIN_QQ_LIST` | △ | `[]` | 超管 QQ 列表，JSON 数组 |
| `SUBFLOW_MAX_TASKS_PER_USER` | | 5 | 单人最大未完成任务数，0=不限 |
| `SUBFLOW_SYNC_INTERVAL` | | 30 | 缓存全量同步间隔（分钟） |
| `SUBFLOW_CONFIRM_TIMEOUT` | | 30 | `/删除任务` 确认窗口（秒） |
| `SUBFLOW_TOKEN_WARN_DAYS` | | 7 | token 距过期多少天开始告警 |
| `SUBFLOW_DATA_DIR` | | `./data` | 运行时数据目录 |
| `SUBFLOW_DEFAULT_PIPELINE` | | 见示例 | 番剧未自定义流水线时的默认 DSL |
| `SUBFLOW_NOTIFY_EXTERNAL_CHANGES` | | `true` | 同步时检测人工直改表格并播报到工作群（D17） |
| `SUBFLOW_EXTERNAL_CHANGE_DIGEST_THRESHOLD` | | 5 | 每群本轮变更 > 此数则汇总成一条，防刷屏（D17） |

---

## 日常运维

### Token 续期（每 25 天一次）

腾讯文档 access_token 30 天过期。Bot 启动时检查：
- 剩 7 天内：日志告警
- 已过期：拒绝相关 API 调用，但 Bot 仍能启动（缓存命令仍可用）

续期流程：
1. 到开放平台后台重新生成 token
2. 改 `.env` 里的 `TENCENT_DOC_ACCESS_TOKEN`（多 key 则改 `SUBFLOW_TENCENT_DOC_KEYS` 数组里对应那套）
3. `docker compose restart nonebot`（`.env` 是文件挂载，restart 即可重读，无需 down+up）

### 提升日额度（多 key 轮换池，D18）

腾讯文档个人开发者额度 2000 次/天，按"开发者应用/账号"计。日常用量约 200~300/天，离上限很远；但若番剧数多、同步频繁，可配多套 key 把额度叠到约 N×2000：

```env
SUBFLOW_TENCENT_DOC_KEYS=[{"client_id":"c1","open_id":"o1","access_token":"t1"},{"client_id":"c2","open_id":"o2","access_token":"t2"}]
```

- **前提**：每套 key 背后是**不同的开发者账号**，且**都已被授权访问同一批智能表**（同账号多签 token 不叠加配额）。
- Bot 每次 API 调用按 round-robin 轮换 key，均摊日额度；某把 key 的 token 失效会被自动剔除、限流则短时冷却，都会自动转移到下一把。
- 启动时逐 key 校验 token：过期/无效的剔除并告警，只要还有 ≥1 把有效就正常启动；全部失效才降级（查询走缓存、写操作报错）。
- `SUBFLOW_TENCENT_DOC_KEYS` 非空时取代单 key 三元组；留空则仍用三元组（向后兼容）。

### 备份

```bash
tar czf subflow-backup-$(date +%F).tar.gz data/
```

`data/` 里只有三个 JSON（绑定、流水线、每集快照），文本可读，丢了也可以重新 `/绑定id` + `/设置流水线`，但已有集的流水线快照丢了会 fallback 到当前流水线（D10 fallback 路径）。

### 手动改腾讯文档

允许 —— Bot 在每次 30 分钟全量同步时会拉到外部改动；写操作走「写后重读」保证缓存最新。
开启 `SUBFLOW_NOTIFY_EXTERNAL_CHANGES`（默认开）后，同步还会 diff 出这些人工直改（改进度 / 填组员 / 标完成 / 增删行），按业务语义在对应工作群播报，并对手动标「已完成」触发下游解锁提示；变更多时合并成一条汇总。检测只读、不回写文档，重启不会把存量行当成变更播报。

不必担心"同步前手动填表被 `/接活` 覆盖"（D19）：接活/完成/放弃/进行中 在写库前都会先重读该行最新远端状态再判定。接活只在「未分配且组员为空」时才写入，否则回「已被占用，已刷新，未覆盖」——你刚在文档里手动认领的行不会被冲掉。（`/修改任务` 是管理员强制，不受此拦截。）

---

## 开发

```bash
# 装可编辑模式 + 测试依赖
pip install -e ".[test]"

# 跑全部测试（含真实 API 集成测试，需要 spike/.env.spike）
pytest

# 只跑离线单测
pytest -m "not integration"
```

测试覆盖 256 项，含：
- 全部 storage / cache / task_manager 业务路径
- D3 提醒文案、D5 并发锁、D6 归一化、D7 二次确认过期、D10 流水线快照隔离、D15 接活/完成前置语义、D16 艾特码渲染、D17 外部变更检测与提醒、D18 多 key 轮换/故障转移、D19 写前重读防覆盖
- 6 项打真实腾讯文档 API 的端到端集成测试

---

## 状态

- [x] M1 — Storage 层
- [x] M2 — Cache 层 + token 检查
- [x] M3 — Bindings / Pipeline / TaskManager 业务核心
- [x] M4 — NoneBot2 命令层
- [x] M5 — Docker 部署

待办：
- [ ] `/绑定 <番剧名>`：需要验证腾讯文档 get_sheets 接口，暂以 stub 提示用 `/绑定id`
- [ ] 自动 OAuth 续期：当前必须手动续 access_token，需要 client_secret（用户后台暂未提供）

---

## License

[MIT](LICENSE)
