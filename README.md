# HuaJiBot.Py

[![CI](https://github.com/nbtca/HuaJiBot.Py/actions/workflows/ci.yml/badge.svg)](https://github.com/nbtca/HuaJiBot.Py/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

将 [HuaJiBot.NET](https://github.com/nbtca/HuaJiBot.NET) 的主要业务插件移植为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，使用 QQ 官方适配器。仓库目录可以叫 `HuaJiBot.Py`；安装到 AstrBot 时插件目录必须叫 `astrbot_plugin_huaji`，保留此名称可沿用上一版插件配置。

当前版本 **0.6.0**。仓库包含 Python 插件源码、配置模板、测试、官方图标资源与安装包构建工具。功能覆盖和已知差异见 [迁移清单](MIGRATION.md)。

## 功能

| 输入 | 行为 |
|---|---|
| `帮助` / `help` | 查看命令 |
| `日程 [周数]` / `schedule` | 默认未来一周，支持负数历史查询，范围 -128～48 |
| `最近日程` / `next` | 最近 30 天的第一项日程及说明 |
| `工单` / `tickets` | 分页读取全部活动工单及详情，按状态显示；需要 `repair.since` |
| `查询` / `mc` | 原游戏桥玩家列表（名称、延迟、世界）；未配置桥接时可用 Java 状态查询 |
| `事件 [加入/聊天/死亡/进度] [开/关]` | 查看事件开关；管理员修改并持久化 |
| `总结 [今天/昨天/前天]` / `summary` | 默认昨天，分段摘要并合并；需事先开启消息记录 |
| `公告 内容` | 管理员在当前群发文本公告，不修改 QQ 群公告栏 |
| `问 问题` / `ai` | 调用 AstrBot 当前模型，单轮文本 |
| `状态` | 查看版本及通知队列状态 |

完整命令词匹配，可加 `/` 或 `hj ` 前缀。`日程 2` 会触发，`明天日程是什么` 不会。普通业务命令不调用模型。非 @ 触发仍要求 QQ 平台下发全量群消息。

后台功能：日历开始前 60 分钟／结束前 5 分钟提醒、社团周预告和次日提醒、维修超期摘要与 NSQ 事件、游戏聊天双向转发、GitHub push/issues/issue_comment 通知、定时群聊日报、带鉴权的 HTTP 推送。默认关闭后台接入，配置目标后才启动。

注册只读 AI 工具 `huaji_calendar`、`huaji_query`，供 AstrBot 原生 Agent 使用；均校验当前会话授权。`问` 命令本身不执行工具循环。

## 安装与配置

1. 安装 AstrBot 4.29.4 或兼容的 4.x 版本，通过 `qq_official` / `qq_official_webhook` 连接 QQ。
2. WebUI 上传 `dist/astrbot_plugin_huaji-v0.6.0.zip`，或将打包文件放入 `data/plugins/astrbot_plugin_huaji`。依赖由 AstrBot 按 `requirements.txt` 安装，保留框架的核心依赖保护。
3. 插件设置填入完整 `allowed_sessions`（`/sid` 返回的 UMO）与 `admin_ids`（官方 UID），并在 AstrBot 的访问控制中允许该会话。空名单不开放任何群。
4. WebUI 的“高级业务配置 JSON”对应 `extensions_json`。把 `config/extensions.example.json` 内容作为起点编辑，所有推送目标必须属于 `allowed_sessions`。保存后重载插件。
5. 配置工单查询时，打开 `tickets_enabled`，填写 `repair.since`，例如 `2026-01-01T00:00:00+08:00`。默认工单接口和日历地址来自原项目公开地址，不包含访问令牌。
6. 测试 `@机器人 日程`、`@机器人 工单`，再测试非 @。不要将两个版本同时作为同一群的全量游戏消息转发入口；`ignored_bot_ids` 可排除旧机器人 UID。

### 后台配置示例

以下片段填入 `extensions_json`；替换 UMO、地址与密钥。其余字段使用默认值。

```json
{
  "calendar_reminders": [
    {"session":"BOT:GroupMessage:OPENID","mode":"whitelist","keywords":["社团","维修"]}
  ],
  "club":{"daily":true,"weekly":true},
  "repair":{
    "since":"2026-01-01T00:00:00+08:00",
    "reminder_enabled":true,
    "reminder_sessions":["BOT:GroupMessage:OPENID"],
    "event_sessions":["BOT:GroupMessage:OPENID"],
    "nsq":{"enabled":false,"address":"nsqd.example:4150","topic":"repair","channel":"huaji-py","secret":"","tls":false}
  },
  "github":{
    "enabled":true,"address":"wss://mq.example","token":"REPLACE",
    "groups":{"club":["BOT:GroupMessage:OPENID"]},
    "routes":{"nbtca/example":"club"}
  },
  "minecraft_clients":[{
    "enabled":true,"address":"wss://game-mq.example","token":"REPLACE",
    "groups":[{"session":"BOT:GroupMessage:OPENID","legacy_group_id":"OLD_GROUP_ID","to_client":true,"from_client":true,"disabled_events":[]}]
  }],
  "summary":{"enabled":true,"scheduled":true,"sessions":["BOT:GroupMessage:OPENID"]}
}
```

`calendar_reminders.mode` 为 `all` / `whitelist` / `blacklist`，匹配标题、说明、地点。`[skip notification]` 不触发提醒；全天事件不发送分钟级提醒，但仍可进入周报／次日提醒。`club.weekly_day` 与 .NET 一致：周日 0、周一 1。

`summary.enabled` 才记录群消息，仅记录列出的会话；默认保留 7 天。`summary.scheduled` 控制定时发送，默认北京时间 00:00 总结昨天；一小时内最多重试 3 次，失败次数跨重启保留。群历史无法从 QQ 补取，开启之前的消息不在总结范围内。

### 推送规则面板

在 AstrBot 左侧插件分组或插件详情页打开“推送规则”。页面沿用 Dashboard 登录鉴权，提供 GitHub 分组与仓库路由、日程关键词过滤和提醒时刻、维修事件及超期提醒、Minecraft 群转发及事件开关、群聊记录与日报时间的可视化编辑。

页面显示 GitHub 连接状态和通知队列数量，目标会话从插件的 `allowed_sessions` 中选择。新增授权目标在插件设置中维护。服务器地址、鉴权令牌、模型参数继续由插件设置管理。

保存会先校验整份规则并持久化，再更新运行中的配置引用，后续新通知立即使用新规则。已入队的通知保留生成时的目标和内容。游戏桥连接与事件回调继续使用更新后的目标配置，面板会同步显示群内 `事件` 命令的开关状态。

页面包含配置版本校验，其他页面或群内命令修改规则后，旧页面保存会收到冲突提示。重新读取后可继续编辑。输入校验和持久化失败会保留原配置。

### GitHubBridge

`github.enabled` 开启后，可通过 `github.address` 和 `github.token` 连接原 ServerlessMQ，也可使用下文的签名 Webhook 入口。`routes` 把仓库全名映射到推送分组，`groups` 填写目标群的完整 UMO。私有仓库需要明确路由，公开仓库可使用 `default` 分组。

恢复的默认筛选规则与 .NET 版一致。push 只广播默认分支，忽略机器人账号、空推送，以及仅修改根目录隐藏文件或 `.github/` 自动化配置的推送。`.vitepress/`、`.storybook/` 等点目录中的应用源码会正常推送。issues 广播 opened、closed、reopened，issue_comment 广播 created。PR 合并卡片显示合并编号与标题、最多 5 条提交及剩余数量。

卡片事件标志采用 GitHub 官方 Octicons。合并 PR 使用紫色 git-merge，单个提交使用灰色 git-commit，多提交推送使用蓝色 repo-push。Issue 打开／重开为绿色，完成为紫色，不计划处理为灰色 skip，评论使用灰色 comment。图标随插件打包，可离线绘制。

`github.cards_enabled` 默认开启，使用 Pillow 绘制中文图片卡片，包含仓库、语言、事件正文、统计及操作者头像。可用 `github.font_path` 指定字体，Linux 部署建议安装 Noto Sans CJK。卡片使用新的绘制实现。Markdown 正文支持基础文本排版，图片显示占位文字。头像以 96 像素下载并缓存 7 天，刷新失败可继续使用已有缓存。彩色 emoji 使用 Twemoji 17 资源，支持 Unicode 表情、肤色、国旗、组合序列及常见短代码。资源保存在插件数据目录的 github-assets 中。正常通知展示卡片图片和短链接。图片发送失败时保留链接，渲染不可用时提供文本通知。

短链在 `github.short_link` 中配置。开启 `enabled`，填写 `api`、`token`，按服务要求填写配对的 `access_client_id`、`access_client_secret`，对应原 Cloudflare Access service token。短链请求失败时使用原链接。转换脚本会保留原 ShortLinkApi/ShortLinkToken/ShortLinkAccessClientId/ShortLinkAccessClientSecret 配置。

卡片路径与正文在同一事务中入队，旧版文本队列可直接升级。重启后继续投递未完成的图片通知。无队列引用且超过 7 天的卡片会在定时清理时删除。

### 从 .NET 导出配置转换

将需要迁移的配置显式导出为以 `Calendar`、`RepairTeam`、`GitHubBridge`、`MessageBridge`、`每日聊天总结` 为键的 JSON 对象。另准备 `{ "旧群号": "BOT:GroupMessage:OPENID" }` 映射文件，然后执行：

```powershell
.venv/Scripts/python.exe tools/convert_config.py dotnet-export.local.json --session-map sessions.local.json --output migrated.local.json
```

脚本不读取运行中机器人的秘密、不打印密钥、不覆盖现有输出文件；缺少群映射直接报错。输出可能包含源配置中的令牌，只写 `.local.json`，已被 Git 忽略。旧 QQ 管理员号码不能映射为官方 UID，须手工添加。

### Minecraft 消息桥

`minecraft_enabled` 开启 Java 状态查询。多实例在 `extensions_json.minecraft_servers` 中配置查询列表，每项包含 `name` 和 `address`，例如 `{"name":"主服","address":"mc.example.com:25565"}`。`查询` 会分别显示各实例的在线人数，单个实例连接失败时仍显示其他实例的状态。原单实例 `minecraft_address` 配置继续可用。

消息互通使用 `minecraft_clients` 的连接和群规则。状态查询列表可以包含多个实例，按游戏侧接入的消息桥转发群聊和事件。消息桥客户端可填写 `name`，用于标识玩家详情所属实例。

查询按实例各显示一次在线人数。有玩家时优先显示消息桥或快照的名单，仅有 Java 状态响应时显示可用的玩家样本。消息桥和服务端快照提供玩家游戏名、UUID、个人延迟及维度。

Forge 1.20.1（47.x）可安装仓库内的只读玩家查询模组。在装有 JDK 17 或更新版本的 Linux 上运行 `bash integrations/forge-player-status/build.sh /path/to/forge/libraries`，将生成的 JAR 放入服务端 `mods` 并重启。模组每 100 个服务器 tick 原子更新工作目录下的 `huaji-player-status/players.json`。将整个快照目录只读挂载给 AstrBot，并在对应的 `minecraft_servers` 项设置 `"player_snapshot":"/run/huaji-gto/players.json"`。超过 30 秒的快照会标为不可用，并回退到状态响应中的玩家样本。

连接参数可从原 MessageBridge 配置转换，目标使用官方群 UMO，`legacy_group_id` 保留游戏桥期待的旧群号。`to_client` 控制群聊转入游戏，`from_client` 控制游戏事件转入群聊。聊天、加入／离开、死亡和进度事件可在“推送规则”面板及群内 `事件` 命令中调整。`查询` 通过原 `get_player_list_request` 协议收集服务器玩家列表。

入服提示使用 QQ 官方 API 的发送回执记录消息 ID。离服时通过群消息撤回接口删除对应入服提示。记录持久化到插件数据库，重载后仍可处理。服务器和玩家分别定位，快速进退、发送过程中的离服、跨服务器同名玩家均独立处理。撤回失败沿用通知队列的退避重试，失败条目可在面板状态中查看。

旧 .NET 入口和官方机器人切换时，应先停止旧入口的同群转发，再启用新入口。原协议没有完整事件 ID，QQ 发送成功到回执落库之间的进程崩溃仍可能导致重复通知。

### HTTP 推送

在高级配置中开启 `push_api` 并设置至少 24 字符的 token，默认只监听本机端口 6186。

- `POST /push`：`Authorization: Bearer <token>`；JSON 为 `{"id":"stable-event-id","session":"完整UMO","text":"通知内容"}`。相同 ID／会话重复提交不会重复入队，第一次内容冻结。
- `POST /github`：配置 `github_secret`（至少 24 字符），验证 GitHub 的 `X-Hub-Signature-256`，使用 delivery ID 去重。必须同时启用 `github.enabled` 并配置路由。

返回 202 代表接受入队，不代表已送达 QQ。远程使用须由已有 HTTPS 反向代理转发到本机接口。

## 可靠性与状态

状态位于 AstrBot `data/plugin_data/astrbot_plugin_huaji/huaji.db`：队列、消息去重、事件开关、日报尝试次数和授权群的聊天记录。无模型密钥写入数据库。多段通知在一个事务中入队；发送失败退避重试，最多 3 次，然后标记 `failed`。失败条目不阻塞后续消息，可通过“状态”查看数量。停止插件会取消任务、关闭 HTTP/WebSocket/NSQ 与数据库。

发送采用至少一次语义：QQ 接受发送后进程若在落库前崩溃，恢复可能重复。NSQ 消息入本地持久队列后才 FIN；本地投递最终失败不会让 NSQ 重新投递，应检查失败队列。已接受的定时提醒内容冻结，不自动追踪后续日程编辑。游戏桥原协议无事件 ID，重连后的服务器重放无法可靠去重。未连接的游戏桥不缓存群消息。

## 开发与验证

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/python.exe tools/build.py
```

`tests/runtime_smoke.py` 需要真实 AstrBot Python 环境与核心包路径；以模拟消息和模型验证命令入口、插件 Web API、消息回执及撤回请求。协议测试使用本机临时 WebSocket、NSQ 和 HTTP 服务。

项目代码按以下职责组织。

| 目录或模块 | 内容 |
|---|---|
| `main.py`、`runtime.py` | AstrBot 入口、命令处理、后台任务和持久化投递 |
| `github_bridge.py`、`card_assets.py`、`octicons.py` | GitHub 事件、卡片、头像、emoji 和官方图标 |
| `transports.py`、`qq_delivery.py` | MQ／NSQ 协议、QQ 发送回执和撤回 |
| `push_rules.py`、`views/push-rules/` | 推送规则 API 与内嵌页面 |
| `business.py`、`services.py`、`summaries.py`、`state.py` | 业务查询、提醒、日报和 SQLite 状态 |
| `config/`、`tools/`、`tests/`、`assets/` | 配置模板、转换与打包工具、测试和授权资源 |

具体迁移范围及差异见 [MIGRATION.md](MIGRATION.md)。

## 许可证与来源

本项目采用 **GNU Affero General Public License v3.0（AGPL-3.0）**，完整条款见 [LICENSE](LICENSE)，原项目来源与署名见 [NOTICE](NOTICE)。发行 ZIP 包同时包含这两份文件。

通过网络提供本项目的修改版本时，请遵守 AGPL 对相应源代码提供的要求。依赖库及 AstrBot 本体保留各自许可证。

彩色 emoji 图形来自 [Twemoji](https://github.com/jdecked/twemoji)，按 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 使用。

状态图标来自 [GitHub Octicons](https://github.com/primer/octicons)，原始 SVG、渲染掩码、固定源版本和 MIT 许可位于 `assets/octicons/`。
