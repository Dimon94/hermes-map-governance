<identity>
每次开始工作先读 repo://CONTEXT.md。使用其中的统一语言。
交互、文档和注释使用简明中文。写短句。使用主动语态。指向具体代码、测试或文档坐标。
证据不足时写 Unknown。推断显式标注“推断”。
</identity>

<project>
定位：Hermes Map Governance 是一个独立安装的 Hermes 治理插件。它在不修改 Hermes core、不复用 Kanban 执行状态机的前提下，为 Map 提供董事会、长期 CEO 会话、重大决策审批和 PM 高管汇报。
产品 Spec 见 repo://docs/specs/hermes-map-governance-spec.md。统一语言见 repo://CONTEXT.md。当前模块、依赖和验证入口见 repo://docs/architecture/current-system-map.md。
真相优先级：运行证据和持久状态 > 源码与测试 > 仓库文档和 accepted ADR > 当前官方文档 > 推理。
</project>

<product_invariants>
GitHub Map Issue 是治理真相。SQLite 只保存 binding、审批执法事实、Outbox 状态和可重建投影。
现有 Hermes Kanban task 与 worker 状态不是治理卡片。任何实现不得修改其语义或存储。
一个 Map 只有一条 canonical 长期 CEO session。System prompt 与 toolset 在会话期保持稳定；Map 数据通过 conversation content 进入。
不向 Hermes core 增加 model tool。只在独立插件内注册按角色收窄的 named toolset。
Profile、session 与 Map authority 从 request-scoped context 解析，不能读取 ambient process identity 作为授权。
Dashboard、Tool、CLI 与 event adapter 必须薄，共用 MapGovernanceApplication 这一应用 seam。
外部写入必须先持久化 intent，并通过稳定 identity、readback 与 reconciliation 恢复；不承诺物理 exactly-once。
PM 与 worker 的 ticket、lane、worktree、commit 和 check 证据留在 delivery plane，不进入 executive board。
Behavioral setting 使用 Hermes/plugin 正常配置，不新增非秘密环境变量。
Worker authority 与 publisher authority 分离；local delivery 不授予 remote publication。
所有权不明确时，不修改或销毁 session、worktree、Herdr resource 或 tracker record。
</product_invariants>

<workflow>
改前读取目标文件最近的 AGENTS.md、直接调用者、公开入口、相关测试和相关配置。
先复用现有 module、type、schema、helper 和 script。再使用标准库、运行时原生能力和已安装依赖。最后才写最少新代码。
一次变更只解决一个可独立验证的语义目标。保留用户的无关改动。
复杂任务先分清现象、根因和设计不变量，再给最小动作。
用户要求“确认后执行”时，只给方案，不修改文件。
完整开发流程见 repo://docs/agents/development-workflow.md。调试闭环见 repo://docs/agents/debugging.md。
</workflow>

<forge>
Issue、代码和 CI/CD 归 GitHub，项目坐标 Dimon94/hermes-map-governance。Issue 约定见 repo://docs/agents/issue-tracker.md。
仓库治理自动化统一走 repo://tools/github-api.sh；认证与 API 操作见 repo://docs/agents/github-api-operations.md。
产品运行时的 GitHub tracker seam 归 `plugin/map_governance/tracker.py`，不能让仓库自动化脚本成为产品依赖。
</forge>

<branching>
主目录只 checkout main。无交付编排的单会话改动可直接在主目录工作。
Wayfinder/交付编排使用一张 Map 一个 integration worktree、一个 ticket 一个 Codex App 或 Herdr worktree；一树一分支一写者。
只有 coordinator 能集成、清理其拥有的 delivery worktree。不要修改所有权不明确的活动 worktree。
main 只接收已验证提交；票收口后移除对应 worktree 与分支。详细规则见 repo://docs/agents/git-branching.md。
</branching>

<architecture>
Module 必须有明确 owner、public interface、依赖方向和测试面。
状态只能有一个 canonical owner。缓存、日志、UI projection 和 HTTP status 不能成为第二真相。
跨进程、外部服务和持久状态必须经过明确 seam；确定性内部逻辑保持直接。
公开合同、持久数据和难回退决定变化时，先检查兼容、迁移、回滚和 ADR。
详细规则见 repo://docs/agents/architecture-standards.md。
</architecture>

<layer_contracts>
合同分 L1-L6，逐层披露。变更命中哪层更新哪层，未命中不动。
落点：L1 = repo://docs/architecture/current-system-map.md；L2 = 最近的 module AGENTS.md；L3 = 核心文件头部注释块；L4-L6 = 代码内注释块。
规则和模板见 repo://docs/agents/layer-contracts.md。格式活例子：repo://tools/github-api.sh。
新增 AGENTS.md 时，同目录创建 CLAUDE.md，内容只写 @AGENTS.md。
</layer_contracts>

<code_style>
写或改代码遵循 repo://docs/agents/coding-standards.md。
局部 AGENTS.md、accepted ADR、外部合同和运行证据优先于通用规范。
规范只约束当前变更和阻断当前正确性的既有问题。
</code_style>

<constraints>
Git 只 stage 语义相关路径。不要使用 git add .。
不提交 secret、环境文件（.env）与一次性 evidence file；其余排除项归 .gitignore。
外部输入在边界校验一次。内部代码使用已校验的类型。
错误必须显式、稳定、可测试。高风险失败采用 fail closed，并给出自然恢复动作。
模型只做分类、起草、摘要、抽取和判断。代码负责确定性转换、路由、重试、状态迁移和成本控制。
</constraints>

<verification>
Plugin discovery、profile/session integration 与 lifecycle test 必须使用临时 HERMES_HOME。
通过 MapGovernanceApplication 公共 seam 断言行为和不变量。先跑 focused check，再跑完整相关 suite。
稳定入口见 repo://docs/architecture/current-system-map.md；不要从活动 task 的临时依赖路径复制命令。
</verification>

<done_definition>
完成前说明触达坐标、实际改动、验证命令和结果。变更命中合同层时，列出更新的 L1-L6；未命中时说明未命中。
非琐碎逻辑至少留下一个在错误实现下会失败的检查。修复缺陷时，同一最小检查必须先失败后通过。
跳过测试时说明原因和剩余风险。
提交、推送、创建 PR 或部署需要用户明确要求。普通实现任务只修改并验证本地工作区。
</done_definition>

<review_standard>
先列 Findings。按严重度排序。每条引用文件和行号。
Standards 轴先审 Coding，再审 Architecture。Spec 轴独立核对需求。
没有 finding 时，也说明测试缺口、未验证路径和剩余风险。
只报告能由代码、测试、运行证据或合同证明的问题。详细检查见 repo://docs/agents/quality-standards.md。
</review_standard>

<principles>
在满足当前需求的前提下，选择最简单的实现。
先交付可端到端验证的最小切片，再逐步增加能力。
新增依赖前检查现有依赖、官方文档和类型定义。
删除废弃的内部路径。持久状态和外部合同的破坏性删除必须先证明零读零写，并取得明确授权。
重复规则和并行机制出现时，读取 repo://docs/agents/entropy-governance.md。
根因诊断、互斥方案和落地推演时，读取 repo://docs/agents/thinking-model.md。
</principles>
