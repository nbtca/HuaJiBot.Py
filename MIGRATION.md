# 迁移清单与边界

源：[nbtca/HuaJiBot.NET](https://github.com/nbtca/HuaJiBot.NET) 的业务插件；目标：AstrBot 官方 QQ 适配器。依据各插件具体命令、DTO 和传输实现迁移，不依赖读取线上服务配置。

| 原模块／源码 | Python 实现 | 验证与差异 |
|---|---|---|
| Calendar/PluginMain、RemoteSync、CalendarExtensions | services.py、main.py | 周数范围、历史／最近日程、时区、重复与例外日期；本地用例验证。长输出限制 15000 字，不静默丢弃后半部分 |
| Calendar/ReminderTask、ClubAffairsReminder | business.py、runtime.py | 60/5 分钟提醒、关键词白黑名单、skip notification、周报／次日提醒；改为持久队列，首次启动只补最近 2 分钟分钟级提醒 |
| RepairTeam/SaturdayClient、TicketDigest、TicketReminder | business.py、runtime.py | 活动状态分页、详情、最后活动时间、1/3/2 天阈值、每天提醒；详情失败会报错而非返回不完整总数 |
| RepairTeam/NsqConnector | transports.py | NSQ V2 IDENTIFY/TLS/AUTH/SUB/RDY/NOP/FIN/REQ、重连；本地协议服务验证心跳与连续收取。TLS 与生产认证仍待真实服务联调 |
| MessageBridge/Types、Commands、PluginMain | transports.py、runtime.py、main.py | 保持 snake_case 包结构、Bearer 头、群消息转发、玩家列表关联、聊天／进退／死亡／进度事件和管理员开关 |
| GitHubBridge/EventDispatch、ServerlessMQ | business.py、runtime.py、transports.py | 兼容 type=webhook/data={headers,body}，push/issues/issue_comment，按仓库映射推送，私有仓库无明确路由不外发 |
| DailySummary | summaries.py、state.py、runtime.py | 明确授权群记录、北京时间日期范围、分段合并、定时重试、超时与清理；未迁入旧 SQLite 历史数据 |
| PushPanel/PushRules | AstrBot _conf_schema.json + configuration.py | 同类业务规则由 AstrBot 配置面板管理，保存后重载；不移植原独立 HTTP 页面和图片模板。额外提供鉴权 /push 与 GitHub 签名入口 |
| AIChat/ExportFunctions | AstrBot 原生模型 + huaji_calendar、huaji_query 工具 | 保留单轮“问”命令，提供只读业务工具。原多模态、引用会话、人设/MCP 由 AstrBot 原生能力承担，未做与 .NET 逐条行为等价验证 |

## 明确不宣称已完成的部分

- 没有移植原 GitHub 卡片图片渲染、短链服务和图标资源；使用文本与原始链接。
- 没有实现玩家退出时撤回“加入服务器”消息；需要核实官方 QQ 的撤回权限及发送回执能力。
- 维修原始 NSQ 内容的 PushRawGroup 调试转发未启用，只转格式化业务事件。
- .NET 的 FlueBridge / huaji-agent 云端工具协议、D1 数据和工作流没有迁入本仓库；它们是另一套运行后端。本仓库使用 AstrBot 的模型与消息通道。
- 保留完整命令词匹配，不做聊天子串匹配或随机 AI 插话。QQ群消息接收与主动推送额度仍由 QQ 平台决定。
- 默认配置不连接生产 NSQ、GitHub MQ、游戏 MQ，也不会开启聊天记录、定时消息或外部 HTTP 服务。
- 本地协议测试、CI 和 AstrBot 加载测试不能代替真实目标服务联调；部署时应按下述步骤逐项验收。

## 建议验收顺序

1. 导入完整 UMO 白名单及 UID 管理员；确认 `帮助` 及 `@机器人 帮助` 都可达。
2. 测试日程正负周数、最近日程；填写 repair.since 后核对完整工单数量。
3. 只接入一个游戏桥，验证 `查询` 中玩家名称／世界，并双向发一条测试聊天；验证 `事件 聊天 关`。
4. 在测试目标启用 GitHub 或 NSQ，确认路由和重复事件行为，验证断线恢复与发送失败队列。
5. 在测试群开启记录和日报；确认不会包含其他群或启用前的记录，再开启定时推送。
6. 按需启用日历提醒与工单超期提醒，核对北京时间和过滤规则。

协议参照：[AstrBot 插件开发](https://docs.astrbot.app/dev/star/plugin-new.html)、[NSQ TCP V2](https://nsq.io/clients/tcp_protocol_spec.html)。AstrBot SDK 以本机 4.29.4 源码为验证基准。
