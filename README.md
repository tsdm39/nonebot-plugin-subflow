# nonebot-plugin-subflow

字幕组任务管理 Bot — 对接腾讯文档智能表，在 QQ 群里管理"接活 / 完成 / 依赖跟踪 / 自动通知"。

> 设计文档：[md/字幕组Bot设计文档.md](md/字幕组Bot设计文档.md)
> 实现方案与决策记录：[md/Bot实现方案.md](md/Bot实现方案.md)

---

## 它能做什么

字幕组日常按"集"推进，每集要走多道工序（翻译 / 时轴 / 校对 / 后期 / 监制 / 压制 等），原本靠手动在腾讯文档智能表里改进度，常漏接、重复接、信息滞后。本 Bot 把这套流程搬进 QQ 群：

- **接活**：群员发 `/接活 淡岛百景 7 翻译 1` → Bot 自动把进度从「未分配」改成「已分配」、写入 QQ 号到「组员」列
- **完成 + 依赖通知**：`/完成` 后 Bot 自动判断下游工序的前置是否全部满足，满足则在工作群通知「校对现在可以接了 → /接活 ...」
- **进度看板**：`/进度 淡岛百景 07` 一键查看本集所有工序的当前状态
- **可定制工序**：每个番剧的工序链通过 DSL 自定义（`/设置流水线 淡岛百景 翻译[分段] → 时轴[分段] → 校对 → 后期 → 监制 → 压制`）；同段串行（P1 翻译→P1 时轴），不同段并行
- **总群 / 工作群分流**：每番一个工作群、一个总群俯瞰所有番；总群只能查询、不接收推送
- **改流水线不影响已有集**：每集创建时快照一份当前流水线，后续改流水线只影响新建集

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

编辑 `.env`：至少填 `TENCENT_DOC_CLIENT_ID` / `TENCENT_DOC_OPEN_ID` / `TENCENT_DOC_ACCESS_TOKEN`；其他按注释提示按需填。
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
- **同段串行**（D13）：当上下游**都标 [分段]** 时，依赖按"同段"判断 — P1 翻译完成后立即解锁 P1 时轴，不影响 P2/P3；若有一方不分段（如下游 `校对`），仍按"所有上游段都完成"判断
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

> D3：`/完成` 全员可执行，但发送者 ≠ 当前组员时会附提醒（`⚠️ 你不是该任务的当前组员（@原组员），已为你标记完成`）。`/放弃` 同。

### 查询（工作群 + 总群均可）

```
/进度 <番剧名> [集数]
/我的任务
/待接 [番剧名]
```

工作群单绑定时番剧名可省；多绑定时省略会让你选。

---

## 配置项（.env）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `TENCENT_DOC_CLIENT_ID` | ✓ | — | 开放平台应用 client_id |
| `TENCENT_DOC_OPEN_ID` | ✓ | — | open_id |
| `TENCENT_DOC_ACCESS_TOKEN` | ✓ | — | 30 天 JWT |
| `TENCENT_DOC_DEFAULT_FILE_ID` | △ | "" | 启用 `/绑定 <番剧名>`（按名查找子表）必填 |
| `SUBFLOW_MAIN_GROUP_ID` | △ | None | 总群 QQ 群号，留空则不启用总群 |
| `SUBFLOW_ADMIN_QQ_LIST` | △ | `[]` | 超管 QQ 列表，JSON 数组 |
| `SUBFLOW_MAX_TASKS_PER_USER` | | 5 | 单人最大未完成任务数，0=不限 |
| `SUBFLOW_SYNC_INTERVAL` | | 30 | 缓存全量同步间隔（分钟） |
| `SUBFLOW_CONFIRM_TIMEOUT` | | 30 | `/删除任务` 确认窗口（秒） |
| `SUBFLOW_TOKEN_WARN_DAYS` | | 7 | token 距过期多少天开始告警 |
| `SUBFLOW_DATA_DIR` | | `./data` | 运行时数据目录 |
| `SUBFLOW_DEFAULT_PIPELINE` | | 见示例 | 番剧未自定义流水线时的默认 DSL |

---

## 日常运维

### Token 续期（每 25 天一次）

腾讯文档 access_token 30 天过期。Bot 启动时检查：
- 剩 7 天内：日志告警
- 已过期：拒绝相关 API 调用，但 Bot 仍能启动（缓存命令仍可用）

续期流程：
1. 到开放平台后台重新生成 token
2. 改 `.env` 里的 `TENCENT_DOC_ACCESS_TOKEN`
3. `docker compose restart nonebot`（`.env` 是文件挂载，restart 即可重读，无需 down+up）

### 备份

```bash
tar czf subflow-backup-$(date +%F).tar.gz data/
```

`data/` 里只有三个 JSON（绑定、流水线、每集快照），文本可读，丢了也可以重新 `/绑定id` + `/设置流水线`，但已有集的流水线快照丢了会 fallback 到当前流水线（D10 fallback 路径）。

### 手动改腾讯文档

允许 —— Bot 在每次 30 分钟全量同步时会拉到外部改动；写操作走「写后重读」保证缓存最新。

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

测试覆盖 210 项，含：
- 全部 storage / cache / task_manager 业务路径
- D3 提醒文案、D5 并发锁、D6 归一化、D7 二次确认过期、D10 流水线快照隔离
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
