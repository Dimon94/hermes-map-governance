# Hermes Map Governance Context

本文件保存项目统一语言。定义来自已批准 Spec、当前实现与验证记录；未交付能力仍标为 Unknown。

## 使用规则

- 代码、测试、Issue、ADR 和文档使用本文件定义的 canonical term。
- 一个概念只保留一个名称。身份、显示名、外部 ID 与本地引用不能混用。
- 新概念必须有运行证据、需求或 accepted ADR 支持。
- 不确定的定义标为 Unknown，不用目标愿景冒充当前事实。

## 已确认词汇

**Map**
: 一项由董事长和 Hermes CEO 治理的长期产品倡议。一个 Map 绑定一个顶层 GitHub Map Issue 与一条 canonical CEO Session。
_Avoid_: implementation ticket、Kanban task、worker lane。

**Map Issue**
: Map 的顶层 GitHub Issue。它拥有治理阶段、结构化决策、审批历史、PM 高管汇报和验收证据的最终解释权。
_Avoid_: 把本地 projection 或 GitHub Project item 当治理真相。

**Governance Plane（治理平面）**
: 董事长审批、CEO 决策、Map 阶段与 PM 高管汇报所在的边界。
_Avoid_: 把 worker 调度与实现日志搬进董事会。

**Delivery Plane（交付平面）**
: Implementation ticket、lane registry、Herdr/Codex/Claude worker、worktree、Git commit 与 check 所在的执行边界。
_Avoid_: Executive board、governance card。

**Executive Board（董事会）**
: 按 Hermes Project 聚合 Map 的高管 read model。它只展示治理阶段、决策、审批、blocker 与验收摘要。
_Avoid_: Hermes Kanban、第二套 ticket board。

**Canonical CEO Session**
: 由不可变 Map identity 和 CEO profile 唯一确定的长期 Hermes 会话。采用 exact lookup 与 adopt-before-mint。
_Avoid_: 按可变标题查找、每次打开新建、动态修改 system prompt。

**Chairman Approval（董事长审批）**
: 对超出 CEO authority envelope 的 action 所作的显式、内容绑定、可过期、可审计裁决。
_Avoid_: 由 CEO 自批、把本地完成视为发布批准。

**Authority Envelope**
: 正常 plugin settings 中定义的 CEO 自主 decision class、董事长必审 class、阈值、actor allowlist 与 TTL。
_Avoid_: 环境变量中的行为策略、由 model 参数声明 authority。

**PM Executive Report**
: PM 写入 Map Issue 的 checkpoint、question、whole-Map blocker、acceptance evidence 或 terminal failure 摘要。
_Avoid_: Pane log、worker status、implementation ticket 明细。

**Map Stage**
: 从 GitHub Issue open/closed 状态、close reason 和唯一 `map-stage/*` label 派生的治理阶段。
_Avoid_: Hermes Kanban running/review 状态。

**Map Binding**
: GitHub Project、Map Issue、request-scoped profile 与 canonical session 等稳定坐标之间的插件自有关系记录。
_Avoid_: 复制 Issue 内容形成第二真相。

**Projection（投影）**
: 从 Map Issue 与 binding 重建的只读董事会数据。允许缓存，但必须标明新鲜度并能重建。
_Avoid_: 反向写入 tracker truth、将 transport success 当投影成功。

**Outbox**
: 插件 SQLite 中持久化的 external effect intent、lease、attempt、retry、ack 与 repair 记录。通过 stable identity 和 downstream readback 收敛到 semantic once。
_Avoid_: 内存队列、物理 exactly-once 承诺、静默删除 terminal failure。

**Application Seam**
: `MapGovernanceApplication`。CLI、Dashboard、CEO Tool、PM Tool 与 lifecycle adapter 共用的治理应用入口。
_Avoid_: 在 adapter 重复 policy 或直接写 canonical state。

## 待确认或待交付

- Live board event stream、cursor/reconnect 与 tracker 不可达时的 stale read-only 细节。
- Setup/doctor 对 Hermes、Herdr、Codex、Claude 和凭据分级的最终诊断合同。
- 真实 Herdr PM commissioning、worker routing、pause/resume/cancel 与 restart repair。
- Acceptance、publisher credential 隔离、remote publication 与最终 closeout 的生产流程。
- 生产部署、版本兼容矩阵与发布渠道。
