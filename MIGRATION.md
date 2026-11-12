# 迁移清单与边界

本项目对照原插件的命令、DTO 和传输协议，迁移到 AstrBot 官方 QQ 适配器。生产参数通过本地配置导入。[源项目](https://github.com/nbtca/HuaJiBot.NET)

| 原模块／源码 | Python 实现 | 验证与差异 |
|---|---|---|
| Calendar/PluginMain、RemoteSync、CalendarExtensions | services.py、main.py | 周数范围、历史／最近日程、时区、重复与例外日期；本地用例验证。长输出限制 15000 字，不静默丢弃后半部分 |
| Calendar/ReminderTask、ClubAffairsReminder | business.py、runtime.py | 60/5 分钟提醒、关键词白黑名单、skip notification、周报／次日提醒；改为持久队列，首次启动只补最近 2 分钟分钟级提醒 |
| RepairTeam/SaturdayClient、TicketDigest、TicketReminder | business.py、runtime.py | 活动状态分页、详情、最后活动时间、1/3/2 天阈值、每天提醒；详情失败会报错而非返回不完整总数 |
| RepairTeam/NsqConnector | transports.py | NSQ V2 IDENTIFY/TLS/AUTH/SUB/RDY/NOP/FIN/REQ、重连；本地协议服务验证心跳与连续收取。TLS 与生产认证仍待真实服务联调 |
| MessageBridge/Types、Commands、PluginMain | transports.py、runtime.py、main.py | 保持 snake_case 包结构、Bearer 头、群消息转发、玩家列表关联、聊天／进退／死亡／进度事件和管理员开关，新增持久化入服消息回执与离服撤回 |
| GitHubBridge/EventDispatch、ServerlessMQ、ShortLinkApi | github_bridge.py、runtime.py、transports.py | 恢复默认分支、机器人账号和 issue 动作筛选，支持点目录源码推送及元数据过滤、PR 合并摘要、中文图片卡片、头像与统计、短链及 Cloudflare Access 鉴权。兼容原 MQ 包和签名 Webhook，按仓库路由并隔离私有仓库 |
| DailySummary | summaries.py、state.py、runtime.py | 明确授权群记录、北京时间日期范围、分段合并、定时重试、超时与清理；原 LiteDB 聊天历史尚待导入 |
| PushPanel/PushRules | push_rules.py、views/push-rules、AstrBot 插件 Web API | 迁移五组规则的可视化编辑，复用 Dashboard 鉴权。保存后更新运行配置，提供输入校验、写盘失败回滚和版本冲突检测。原 /api/rules 由插件扩展 API 承接 |
| AIChat/ExportFunctions | AstrBot 原生模型 + huaji_calendar、huaji_query 工具 | 保留单轮“问”命令，提供只读业务工具。原多模态、引用会话、人设/MCP 由 AstrBot 原生能力承担，未做与 .NET 逐条行为等价验证 |

## 尚待迁移

- FlueBridge / huaji-agent 的签名入站、Cloudflare Queue 回执、远程命令、D1 数据及工作流。
- 原 AIChat 的图片处理、引用追问和历史会话重建。
- 日期计算、ASCII 艺术字与自动收集各插件 ExportFunctions 的 AI 工具机制。
- 原 LiteDB 聊天记录、AI 会话与日报状态的数据导入。

## 已知差异与验收范围

- GitHub 卡片采用 Pillow、官方 Octicons 和彩色 emoji，支持基础 Markdown 文本排版，完整富文本样式仍有差异。
- 日程输出保留简化格式，完整星期、相对日期和描述链接排版尚待恢复。
- 入服回执和离服撤回已经实现，实际送达效果需要按目标群进行进退服验收。
- 维修原始 NSQ 事件通过 repair.raw_session 配置，可在推送规则视图中编辑。
- 默认后台配置由部署者填写。NSQ 生产认证、各目标群的收发权限及定时任务需要结合部署环境验收。
- 本地协议测试、CI 和 AstrBot 运行库检查覆盖可重复验证的行为，生产验收按下述步骤进行。

## 建议验收顺序

1. 导入完整 UMO 白名单及 UID 管理员；确认 `帮助` 及 `@机器人 帮助` 都可达。
2. 测试日程正负周数、最近日程；填写 repair.since 后核对完整工单数量。
3. 只接入一个游戏桥，验证 `查询` 中玩家名称／世界，并双向发一条测试聊天；验证 `事件 聊天 关`。
4. 在测试目标启用 GitHub 或 NSQ，确认路由和重复事件行为，验证断线恢复与发送失败队列。
5. 在测试群开启记录和日报；确认不会包含其他群或启用前的记录，再开启定时推送。
6. 按需启用日历提醒与工单超期提醒，核对北京时间和过滤规则。

协议参照：[AstrBot 插件开发](https://docs.astrbot.app/dev/star/plugin-new.html)、[NSQ TCP V2](https://nsq.io/clients/tcp_protocol_spec.html)。AstrBot SDK 以本机 4.29.4 源码为验证基准。

QQ 群消息撤回接口参考 [QQ机器人API](https://tencentbot.apifox.cn/387846691e0)。
