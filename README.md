# Hermes Map Governance

Hermes Map Governance 是一个独立安装的 Hermes 治理插件。它把 Hermes 作为长期负责一个 Map 的 CEO，把董事长审批、CEO 决策、PM 提问和验收证据保留在治理平面，同时让 implementation tickets、worker lanes、Git 和 CI 继续承担执行真相。

项目目标是提供：

- Hermes Desktop 中独立的 Maps / 董事会页面；
- 每个 Map 唯一、长期且可恢复的 Hermes CEO 会话；
- CEO 授权边界与董事长重大决策审批；
- Hermes PM 对既有 `delivery-pipeline` 与 Herdr 的编排；
- GitHub Issue 单一治理真相源；
- 与 Hermes 核心 Kanban 执行状态机隔离的独立插件架构。

当前交付包含可安装插件壳、GitHub Project / Issue adapter、Map binding registry、
可重建董事会投影，以及 native CLI、dashboard API 和 Maps 页面。治理 transition 会先
原子替换 GitHub 的唯一 `map-stage/*` label、读回确认，再按 expected stage 提交本地
projection。每个 Map 现在会在显式 CEO profile 中 exact-adopt 或创建一条 canonical
Hermes 会话，并在首次创建时以 user turn 写入 Map bootstrap。插件注册显式
`map-governance:ceo` Skill 与固定 `map-governance-ceo` toolset；canonical CEO 会话可读取
executive state，并把带稳定 `decision_id`、type、rationale、authority、affected stage 和
timestamp 的结构化决策写入 Map Issue comment。插件还独立注册 `map-governance:pm`
Skill 与 `map-governance-pm` toolset。PM request 只能使用持久 assignment 解析其 Map，
并可提交 checkpoint、question、blocker、acceptance evidence 和 terminal failure；真实
PM contract 固定 `dispatch_runtime: herdr`。显式 commission 会在 plugin-owned Herdr
session 中创建或恢复该 Map 唯一的 workspace 与 root Hermes PM。`delivery-pipeline`
准备一张 ticket 的 Map Integration / Execution Worktree 与 durable lane registry 后，PM 通过
bounded `map_governance_pm_dispatch` bridge 按 authoritative ticket attributes、repository
policy、integration readiness 与 configured default 派发一个 Codex 或 Claude worker；下一次
coordinator turn 使用 byte-identical lane payload 收集一个本地 commit、幂等 cherry-pick、运行
declared focused validation，并写入 blocker 或 acceptance recommendation。启动 worker 前会把
Herdr 坐标写入 implementation ticket 的 `wayfinder-lane-registry:v1` created checkpoint 并 readback；
确认 prompt handoff 后写入 running，fan-in 再依次 readback blocked 或 terminal / integrated
checkpoint。该链路不会 push、创建
PR、merge、release、关闭 Issue 或清理 lane。
新建或既有 canonical lineage 都会在当前 live continuation 幂等加载完整 CEO Skill user
turn，因此从 #6 升级和 context compression 后仍保留同一 negative-capability 边界。
CEO 还可提交内容绑定的 chairman approval packet；Dashboard Map detail 只提供显式
approve、reject、request revision 操作。审批 request/decision 先由 GitHub Issue history
读回确认，再进入 plugin-owned durable ledger。`awaiting-approval → authorized` 会在执行
同一 application transition 前原子校验并消费匹配 action、scope 与 payload 的有效审批。

## 安装

```bash
hermes plugins install Dimon94/hermes-map-governance --enable
hermes maps health
hermes dashboard
```

Dashboard 导航中会出现 **Maps**。尚未绑定 Map 时，页面显示空状态，而不是创建
Hermes Kanban task。插件数据位于当前请求 profile 的 Hermes 原生插件存储命名空间
（`<PROFILE_HOME>/plugin-data/<map-governance-native-namespace>/registry.db`）；
更新或卸载插件不会把它误当作安装文件删除。

本仓库是 standalone plugin，安装到
`<HERMES_HOME>/plugins/map-governance/`，不要求也不会修改 Hermes core。

## 显式 setup 与纯读 doctor

`maps setup` 只管理普通 Hermes/plugin behavioral configuration，不读取当前 shell
来猜 profile、Skill、repository 或 authority，也不接收 secret。GitHub credential 只以
`gh:<host>:<account>` provider/auth-context reference 表示，而且必须与所选 host/account
一致；token 仍由 `gh`/keychain 持有，并且 privileged subprocess 只读取 setup 中显式、
owner-only（0700）、非 symlink 的 `gh_config_dir`，不继承 worker/PM 的 ambient `gh` 登录。
启用 publication 时还必须声明两个真实存在且 UID 不同的 worker/publisher OS service user，
以及两者共同加入的 `control_group`。worker 首次 composition 会把 worker-owned plugin storage、
配置和 SQLite durable state 固定为仅该 control group 可读写的精确 ownership/mode；publisher 只接受该契约，
不会复制或另建 approval ledger、Outbox 或 closeout truth。
CEO/PM/worker composition 必须由 worker user 启动，privileged CLI 必须由 publisher user
启动；两侧运行时都会核对实际 euid。Git 与 GitHub CLI 必须
使用 setup 固定的 absolute、root/publisher-owned、不可 group/world write executable。push
不让 publisher 对 worker checkout 运行 Git：acceptance 提交时，worker composition 使用独立的
`worker.git_executable`，在 shared control storage 的 `publication-handoffs/` 内以隔离 bare
staging 原子生成并校验 content-addressed、worker-owned/control-group-readable handoff；
publisher 以 `O_NOFOLLOW` 校验真实 UID/mode/link count，复制到 publisher-owned bare staging
并校验 exact commit。远端 push 再禁用 hooks、ambient Git config、credential helper 与非 HTTPS
transport，因而不会在持 credential 的进程中执行 worker checkout 里的 hook、config 或 program。
正常结果会立即删除 action staging；进程崩溃残留只保留 24 小时并按严格 owned-name/mode GC。
可从 [`docs/setup-example.json`](docs/setup-example.json) 复制一份 JSON
desired-state 文件，内容包含：

- 彼此不同的 CEO、PM 与 CLI-only publisher profile ID；publisher profile 不得启用
  CEO/PM model toolset；
- plugin Skills `map-governance:ceo`、`map-governance:pm`，以及既有外部 owner
  Skills `delivery-pipeline`、`implement` 的绝对 `SKILL.md` 路径；
- `codex`、`claude` 或 `mixed` routing policy；
- routing default、missing-integration 的 `blocked` / `fallback` 行为，以及每个 repository 的
  attribute-to-worker policy；
- 已选择的 GitHub Project identity、repository coordinate 与本地 repository path；
- `local_git` worker authority、worker-owned/root-owned Git executable 和独立的 publisher
  provider reference；
- 不同的 worker/publisher OS service user、共同的 `control_group`、publisher-only
  `gh_config_dir`，以及固定的
  `git_executable` / `gh_executable`；
- Herdr executable 名称或绝对路径。

计划阶段不写入任何状态，并返回稳定 action ID、before/after、operator authority、config
revision 与过期时间：

```bash
hermes maps setup plan --file map-governance-setup.json > setup-plan.json
```

只有被 operator 明确选择的 action 才会执行：

```bash
hermes maps setup apply \
  --file setup-plan.json \
  --action config.prerequisites
```

apply 会拒绝未知 action、过期/过时 plan 和 payload drift；behavioral configuration 写入当前
profile 的 plugin-owned `prerequisites.yaml`，由所有 setup writer 共享同一 lock，再做二次
revision compare、单次 atomic replace 和 readback；它不与 Hermes 主 `config.yaml` 争用或
覆盖更新。安装 profile、Skill、credential 或 Herdr integration
不隐藏在 apply 中；doctor 只会给出需要人工执行的 argv remediation。

```bash
hermes maps doctor
```

Doctor 为每项 prerequisite 返回独立 `pass`、`warning` 或 `fail` evidence，包括 profile
隔离、Skill discovery、现有 storage owner/mode 与 SQLite immutable read-only open、GitHub
auth/Project/repository capability、registry coordinate cross-check、local worker write authority、
单独的 publisher authority，以及 Herdr version 和 Hermes/Codex/Claude integration state。
worker 身份只能把完整且隔离的 publisher 配置报告为 `warning`，不会用自己的 ambient GitHub
权限冒充 publisher 的 live `pass`；privileged publisher composition 会在每次远程变更前，以
publisher-only `gh_config_dir` 验证 exact account 及每个 allowlisted repository 的写权限。
它不创建 storage、DB、journal、lock，不安装 integration，不修改 profile/config/credential，
也不会输出 subprocess stderr 或 credential body。缺 publisher credential 不会让 local
execution worker 失败；只有明确要求 publication 的 repository 才把 publisher 缺失判为
fail。Maps 页面提供同一 read-only Doctor report，也提供 desired JSON、secret-safe plan preview、
逐 action ID 选择和 verified apply；REST endpoints 同样要求显式 action ID。

## 绑定已有 Map

先确保 `gh auth status` 可用且 token 具有 `read:project` scope，并且开放中的 Map
Issue 恰好有一个受支持的 `map-stage/*` 标签。绑定流程只读取 GitHub Project 与
Issue，不创建或复制 Issue，也不会向 GitHub Project、Hermes Kanban 或
implementation ticket 写入内容。

```bash
hermes maps project configure \
  --url https://github.com/orgs/OWNER/projects/PROJECT_NUMBER

# 使用上一条命令返回的 Project node id。
hermes maps bind \
  --project PROJECT_NODE_ID \
  --issue https://github.com/OWNER/REPOSITORY/issues/ISSUE_NUMBER

hermes maps board
hermes maps refresh --project PROJECT_NODE_ID

# --from 必须是发起请求时卡片显示的 stage；并发变化会要求 refresh/retry。
hermes maps transition \
  --map ISSUE_NODE_ID \
  --from authorized \
  --stage delivery

# chairman-protected transition 还必须带稳定 request 与 mutation identity。
hermes maps transition \
  --map ISSUE_NODE_ID \
  --from awaiting-approval \
  --stage authorized \
  --approval-request APPROVAL_REQUEST_ID \
  --mutation-id STABLE_MUTATION_ID

# 解析/初始化 canonical 会话；Dashboard 卡片使用同一 application seam。
hermes maps open --map ISSUE_NODE_ID --profile CEO_PROFILE

# 显式 commission/resume 与状态检查都要求 canonical CEO request identity。
hermes maps commission \
  --map ISSUE_NODE_ID \
  --profile CEO_PROFILE \
  --session CANONICAL_CEO_SESSION_ID

# 显式 resume 是同一幂等 seam 的命令别名。
hermes maps resume \
  --map ISSUE_NODE_ID \
  --profile CEO_PROFILE \
  --session CANONICAL_CEO_SESSION_ID

hermes maps runtime status \
  --map ISSUE_NODE_ID \
  --profile CEO_PROFILE \
  --session CANONICAL_CEO_SESSION_ID
```

重复执行同一条 bind 命令是幂等的。`refresh` 从 GitHub tracker truth 与最小 binding
registry 重新生成缓存；删除 projection rows 不会丢失 Map 绑定。

开放 Map 的治理流程为：`discovery → awaiting-approval → authorized → delivery`；
`delivery` 可进入 whole-Map blocker 的 `decision` 或 `acceptance`，两者可返回
`delivery`。任一 active stage 可进入 `parked`，恢复时回到 `discovery` 重新评估。
`done` 与 `cancelled` 不通过 label transition 请求，而分别投影自 GitHub Issue 的
completed 与 not-planned 关闭原因。Dashboard 使用服务器返回的合法下一阶段按钮，
GitHub 写失败时保留旧卡片并显示错误。

Canonical 会话使用不可变 GitHub Issue node id 派生的 exact title。唯一匹配会优先
adopt；零匹配只创建和 bootstrap 一次；多个 exact 匹配会把卡片标为
`repair_required`，不会猜测或删除会话。Registry 保存 lineage root，打开时通过 Hermes
公开 session contract 解析当前 compression tip，因此重复点击、renderer reconnect、
backend restart 与 context compression 都继续同一段历史。Map 内容只写入首个 user
turn；board refresh 不更新已有会话的 system prompt 或 toolset。

CEO Tool 提供 `inspect`、`record_decision`、`request_approval`、显式 `commission` / `resume`
与 `runtime_status`。Profile、session 与
chairman identity 不属于模型参数；
handler 使用 Hermes request-scoped identity 回查 canonical binding。跨 profile、session 或
Map 请求会 fail closed，并写入 plugin-owned denial audit，不会产生 tracker governance write。
canonical CEO 会话还通过 Hermes `pre_tool_call` 公共 hook 拒绝该 named toolset 之外的
工具执行；专用 CEO profile 的可见 toolsets 仍由 Hermes 正常 profile 配置负责，后续
setup/doctor 票负责自动校验该配置。
当前 tracker adapter 按 parent spec 的 prototype 边界使用运行插件进程中已认证的 `gh`
身份；GitHub collaborator 写入的结构化 Issue history 本身就是治理真相。角色 toolset、
canonical request policy 与审计在工作流层隔离 CEO 和 worker 权限，生产级凭据拆分属于
后续安全加固，不能把本地交付误当作远程发布授权。
记录决策时 `authority` 必须为 `ceo`，其权限来自已认证的 canonical CEO request identity，
不能由模型声明其他角色权限。
决策 mutation 后必须从 Issue history 读回同一 payload 才会更新本地 projection；同一
`decision_id` + 同一 payload 的重试只保留一条 tracker decision，不同 payload 会被拒绝。
Maps 卡片显示 confirmed decision、approval 与 PM executive report 摘要；页面的 Map detail 显示 packet
alternatives、rationale、cost/risk、scope、evidence、payload hash、status 与 expiry。
Approval 的 pending/approved/rejected/revision/revoked/expired/consumed 生命周期及历史
保存在 ledger；缺失、过期、撤销、已消费或 action/scope/content 不匹配均在 tracker
mutation 前拒绝并写入既有 denial audit。相同 mutation id 可恢复重试，另一个 mutation
不能重放同一 grant。PM report 使用稳定 record id 与 `map-governance:pm-report:v1`
machine marker；tracker history 读回确认后才更新 delivery summary、badge 与允许的治理阶段。
同 payload 重试幂等，同 id 异 payload 冲突。non-blocking question 保持 `delivery`；只有
whole-Map question/blocker 才可投影 `decision`，terminal failure 只形成高管可读报告，acceptance evidence 只会
请求进入 `acceptance`，不会自行批准、关闭或发布 Map。badge 只表示最新 confirmed PM
状态；后续 checkpoint、acceptance 或 failure 会取代已处理 question/blocker 的 pending badge，
完整历史仍由 Issue comment 与 Map detail 保留。

结构化 acceptance 绑定一个完整 Git revision，并列出 delivered scope、validations、known
limitations、rollback considerations 与一个 exact remote action/target。PM 和 worker 到此为止；
它们拿不到 publisher credential，也不能把 acceptance 直接投影为 `done`。Chairman approval
必须匹配最新 acceptance report identity、revision、target、content hash 与 setup 绑定的
publisher authority reference；内容、revision、target、publisher host/account、reject、revoke
或 expiry 任一变化后旧 grant 都不可重放。独立 privileged publisher 只接收该最小 approved
action，不接收 PM/lane/prompt 状态；Map detail/CEO inspect 会从当前 acceptance 与 setup authority
派生 secret-free 的 exact approval scope/payload，董事长无需读取或猜测 setup。其 publication 与
tracker adapter 复用同一个已 resolve/校验的 setup 固定
`gh_executable`、publisher-only `gh_config_dir` 和 host，不继承 ambient token/config。成功后先把
`map-governance:publication:v1` immutable
remote evidence 写入 Map Issue history，再通过同一 Outbox 关闭 Issue 为 `completed` 并派生
`done`；取消则必须使用 cancellation grant，以 `not_planned` close 派生 `cancelled`。任何
remote partial failure 只写 repair-required governance incident，不自动重做未知是否已成功的
动作，也绝不 close Issue。授权有效性绑定 durable remote-call marker 的 `attempted_at`；provider
回读的 `published_at` 只是 immutable observation time，延迟回读不会把一次已授权尝试误判为
过期重放。readback 必须区分 exact match、confirmed absent 与 drift/ambiguous；后两种分别形成
不可重放的 aborted closeout 或保持 repair-required，且 incident tracker-confirmed 后才可记录
resolution。approval 在最后 preflight 后、remote-call marker 写入前会用同一个
`attempted_at` 再核验 consumption、expiry、latest acceptance revision 与 target，避免检查与执行
之间的 expiry/content TOCTOU。

Publisher 只通过显式 privileged CLI composition 启用；普通 CEO/PM application 与 toolset 不会
注入该 capability。CLI 在执行前核对 setup-owned repository allowlist、配置的 GitHub account、
exact revision 与 target：

```bash
hermes maps publish --map MAP_ID \
  --control-profile CEO_PROFILE \
  --approval-request APPROVAL_REQUEST_ID \
  --mutation-id STABLE_ACTION_ID
```

不确定的远端结果只能用同一 action 的 provider readback 修复；该命令不会再次执行远端 mutation：

```bash
hermes maps reconcile-publication --map MAP_ID \
  --control-profile CEO_PROFILE \
  --action-id STABLE_ACTION_ID
```

PM question 还会持久化 Map-bound scope、blocking impact、evidence、options、decision class
与 stable correlation id。tracker（以及 whole-Map blocker 的 stage）确认后，Outbox 才向该 Map
canonical CEO session 投递一次 correlation-bound turn。CEO 通过同一 application seam 回答：
authority envelope 内写一次 structured decision，外部决策自动形成 chairman packet；approve、
reject 或 revision 都先写回 Issue 并更新 approval ledger，才向经过 ownership 验证的 idle PM
root 投递引用 committed record 的 concise resume。PM resume 后重新读取 tracker history，ack
同一 correlation；continue 才解除 whole-Map `decision`，rejected/revision/blocked 继续保持阻塞。

PM assignment 固定绑定 request-scoped profile/session 与一个 Map，不读取 process env。
受控 coordinator application seam 以 `idle → active → idle` 运行；tracker-confirmed report
或确认的 dispatch 结束后必须 idle，失败的 report 保持同一 active turn 供同 payload 重试，
重启后可安全 resume。acceptance outcome evidence 可出现在 Map detail，但 worker checks、
commands 与 lane activity 仍只属于 delivery artifacts。该 seam 不创建 implementation card，也不保存 pane、
worktree、lane 或 worker log。

一个 delivery turn 只接受 `delivery-pipeline/herdr-implementation-v1` 的单 lane contract：
implementation ticket、resolved `implement` owner、Map Integration Worktree、独立 Execution
Worktree、Base commit、policy-selected Codex/Claude worker kind、显式 integration
order/total/predecessors、固定 validation argv 与
`one-local-commit-integrated-and-validated` completion contract。dispatch readback 后 PM 立即 idle；
terminal evidence 唤醒的后续 turn 才 collect。collect 前会比较 byte-stable dispatch identity，拒绝
payload drift；ticket 必须有 implementation label、精确 Spec `Parent` 回链，且 `Blocked by` 依赖均已
closed。Git 必须证明同一 repo、两个 clean registered worktrees、exact branches 与一个 child
commit，而且 common Git dir 必须属于 configured Map repository。worker terminal 时做一次 bounded
Herdr final-report read，从有界 transcript 中只解析并核对最后一个完整 report 的 successful status、
commit、checks、review、dirty state 与 touched files 后保存 digest；final-report transport 缺失、截断
或字段不完整时，只允许从 tracker registry 加 exact Git 证据恢复，明确的 failed/blocker/negative
evidence 则 fail closed。Map-local integration 从
Base 到 HEAD 只允许这一张 lane patch，validation 后再次验证 HEAD
未改变且 clean；
任何重叠写入或 check side effect 都 fail closed。成功只把 sanitized outcome-level evidence、
limitations 与 acceptance recommendation 写到 Map；board 不呈现 ticket、pane、worktree、commit 或
check command。缺少所选 worker integration 时，在任何 lane mutation 前按配置 fallback 或写入
明确 whole-Map blocker；capacity saturation 与 provider rate limit 只作为 adapter 后的 retryable
outcome 保留同一 created lane。worker blocker 的原始安全摘要只持久化到 ticket registry，Map PM blocker report
始终使用不含 lane detail 的高管摘要；只在 Map 经治理流程回到 delivery 后才允许向同一 worker
发送有界 resume prompt。resume 必须重新核对同一 registry、
Herdr pane occupants 与 Git ownership，并在 `blocked -> running` 时清除旧 blocker receipt。
Codex 与 Claude 都使用 delivery-pipeline canonical `bootstrap_authority: none`；Claude lane
固定使用 `dangerously-skip-permissions` agent mode。Routing policy 只约束 future dispatch，active
registry 始终沿原 worker kind 恢复，不会 migrate、restart 或 terminate。升级前没有
`dispatch_id` 的 active registry 继续使用其原始 #14 packet identity；新 order 字段不会使既有
worker 漂移。晚于前序集成才 dispatch 的 sibling 会从 tracker-confirmed contiguous predecessor
commit frontier 启动，Execution Worktree 仍保持共同 Base，最终 integration chain 逐 commit 验证。

Commissioning 使用稳定、冲突安全的 plugin lifecycle namespace。任何同名但缺少本地
ownership proof 的 Herdr session 或 workspace 都会返回 `repair_required`，不会 attach、删除
或猜 opaque ID。Registry 只保存恢复所需的 session/workspace/window/pane/agent、Map、profile、
repository coordinate 与 lifecycle identity，不保存 terminal 内容、argv、secret 或执行日志。
重复和并发 commission 会恢复同一 root PM。只有 PM 提交预期的 structured ready checkpoint，
且 GitHub Issue history 读回确认同一 payload 后，application 才用既有 Outbox transition 把
Map 从 `authorized` 移到 `delivery`；tracker 未确认或 transition 失败时 runtime 可重试，但
不会宣告 active delivery。

Tracker governance writes、canonical session resume 与可控 coordinator resume 会先写入
plugin-owned SQLite Outbox，再领取有期限的 durable lease 后调用外部边界。稳定 effect id
与 payload hash 拒绝同 id 异 payload；每次调用前先从 GitHub marker、Hermes
`platform_message_id` 或 coordinator turn marker 读回，因此 “外部成功、ack 前崩溃” 的
重启只会收敛到一次语义效果，而不承诺物理 exactly-once。可重试错误按正常 plugin
settings 中的 `outbox` 配置退避；terminal 错误停止自动调用，并在 Map 卡片/detail 暴露原因：

```yaml
plugins:
  entries:
    map-governance:
      settings:
        outbox:
          lease_seconds: 30
          poll_seconds: 1
          base_retry_seconds: 5
          max_retry_seconds: 300
          max_attempts: 5
```

Operator 可用 `hermes maps outbox status --effect EFFECT_ID` 检查 attempt history，
`hermes maps outbox recover --limit 100` 运行到期 action，并用带稳定 repair id 和说明的
`hermes maps outbox repair --effect EFFECT_ID --repair-id REPAIR_ID --note NOTE`
显式重排 terminal intent。Repair 只增加审计记录并重置执行状态，不删除历史或静默吞掉失败。
`publisher.execute` 明确排除在通用 repair 之外，必须使用上述 evidence-only reconciliation。

## Restart recovery 与 identity repair

Dashboard backend 使用完整运行时组成时会从同一 `registry.db` 自动执行 restart recovery：
重新读取 GitHub truth 构建所有 bound Map 投影，按已记录的 root lineage reconnect canonical
CEO session，验证 Map、repository、profile 与 plugin lifecycle 后 rediscover owned Herdr runtime，
并按 durable lease/idempotency 状态继续 Outbox work。它不会 mint replacement CEO session，
也不会 attach、kill 或删除 ownership 未经验证的 pane/session。可显式查看同一恢复报告：

```bash
hermes maps recover --profile CEO_PROFILE --limit 100
```

CEO lineage 或 Herdr coordinate 缺失、重复、冲突时会保持 `repair_required` 并保留 evidence。
Identity binding repair 是独立的 preview/apply 流程；preview 只列出当前证据唯一且 ownership、
Map binding 均已验证的 safe action，apply 只接受 operator 明确选择的 action，并把认证
authorizer、before/after 和 evidence 原子写入审计表：

```bash
hermes maps repair preview --profile CEO_PROFILE > repair-plan.json
hermes maps repair apply \
  --profile CEO_PROFILE \
  --file repair-plan.json \
  --action SAFE_ACTION_ID \
  --authorizer AUTHENTICATED_OPERATOR_ID
```

若 preview 后事实变化，apply 会要求重新 preview。该流程只修改 plugin-owned identity
binding；不会 close Issue、rewrite tracker history、delete session 或清理任何外部 runtime。
Dashboard 提供同样的 preview、逐项选择和确认 apply，REST authorizer 始终来自已认证请求，
不接受客户端自报身份。`maps doctor` 以 immutable read-only SQLite 检查 recovery registry，
发现 repair-required 或未完成 Outbox work 时只给出 remediation，不触发恢复或修复。

Maps 页面先读取一次带 cursor 的完整 projection，随后只通过 Hermes plugin WebSocket
消费 SQLite 中已经提交的 ordered board events，按 project/card/detail reducer 局部更新；
它不会按卡片轮询，也不会在每个 event 后重新加载 application。Cursor 跨进程和重启有效，
断线后从最后 cursor catch up。超出 retention 的 cursor 会收到明确的
`refresh_required`，页面只做一次完整 projection refresh，再从新 cursor 续接。连接资源
有界，慢 consumer 不持有 writer transaction；落后超过 retention 时同样转为 full refresh。

Event journal 保存在同一 plugin-owned `registry.db`，不是内存 event bus。所有 stage、
decision、approval、canonical session、PM report/turn 与 Outbox 可见状态都在其本地状态
transaction commit 后才追加 durable envelope；回滚状态不会产生 event。可用正常 plugin
settings 调整保留与单批上限：

```yaml
plugins:
  entries:
    map-governance:
      settings:
        events:
          max_events: 10000
          retention_seconds: 86400
          batch_size: 200
          poll_seconds: 0.25
          send_timeout_seconds: 5
```

GitHub authority 读取或写入失败只会把受影响 project 标成 stale；最后一次成功 UTC
projection 仍可读，其他 healthy project 仍可治理。Stale project 的 CLI、REST、Dashboard、
CEO Tool 与 PM Tool mutation 全部 fail closed，且不会提供 local-only transition。仅 renderer
WebSocket 断线会显示“live updates disconnected”，不会把 project 标 stale。恢复 GitHub
连通性后运行 `hermes maps refresh --project PROJECT_NODE_ID`；只有 project、所有 bound
Issues 及其 decision、approval 与 PM history 完成 authoritative fetch，并在单一 transaction
reconcile 成功后，stale 才会清除。仅重连或本地 cache 存在不足以恢复写权限。

Authority envelope 使用正常 Hermes plugin settings，不读取进程环境。默认 `product` 与
`operational` 属于 CEO autonomy；delivery authorization、budget、scope、material
schedule、security/legal、cancellation、publication 与 final acceptance 要求 chairman。
可在 profile 的 `config.yaml` 中覆盖并设置审批 TTL：

```yaml
plugins:
  entries:
    map-governance:
      settings:
        authority:
          ceo_autonomous_decision_classes: [product, operational]
          chairman_required_decision_classes:
            [delivery_authorization, budget_increase, scope_expansion,
             schedule_change, security, legal, cancellation,
             remote_publication, final_acceptance]
          chairman_actor_ids: ["basic:local-chairman"]
          authority_thresholds:
            budget_increase:
              source: decision_payload
              field: amount
              maximum: 1000
            schedule_change:
              source: decision_payload
              field: days
              maximum: 5
            scope_expansion:
              source: requested_scope
              field: area
              allowed_values: [existing-map]
          approval_ttl_seconds: 86400
```

`chairman_actor_ids` 使用认证 Dashboard request 的 `provider:user_id`，默认空集；只有
明确列入当前 profile 配置的身份能执行审批。阈值只缩小原本 chairman-required class
中需要中断的范围；缺字段、无效值和未配置 decision class 都按 fail-closed 处理。

## 架构壳

- `MapGovernanceApplication` 是 REST、dashboard 和诊断入口共同调用的应用 seam。
- `hermes maps health` 与 `/api/plugins/map-governance/health` 返回同一 readiness。
- `/api/plugins/map-governance/board` 提供按 GitHub Project 分组的 board 投影；
  `/projects`、`/bindings`、`/refresh`、`/transitions`、approval decision、Map detail 与
  authenticated cancellation、canonical session open routes 是同一应用接口的薄适配器。
- `/api/plugins/map-governance/events` 使用 Hermes 公共 WebSocket auth/upgrade contract，
  提供 durable cursor catch-up、live tail 与 cursor-expiry refresh protocol。
- `registry.db` 属于 `map-governance` 命名空间，由请求中的 profile 选择，且与
  `<PROFILE_HOME>/kanban.db` 隔离。

## 开发验证

集成测试需要一个 Hermes checkout，并且所有运行状态都创建在临时
`HERMES_HOME`：

```bash
HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest
node --check dashboard/dist/index.js
```

测试会从一次性本地 Git repo 执行真实的安装、native/dashboard 发现、页面打开和
卸载流程，不读取开发机现有 Hermes profile。

真实 Herdr mixed-worker smoke 不属于默认门禁，也不会被 pytest 发现。它必须由 operator
显式 opt-in，并要求一个已经发现 `map-governance:pm`、`delivery-pipeline`、`herdr` 的隔离
PM profile，以及绝对的 `implement/SKILL.md` 路径：

```bash
PYTHONPATH=. python tests/real_herdr_mixed_smoke.py \
  --confirm-isolated-real-herdr \
  --pm-profile ISOLATED_PM_PROFILE \
  --implement-skill /absolute/path/to/implement/SKILL.md
```

脚本只创建一次性本地 Git repository/worktrees 与随机 plugin lifecycle 所派生的唯一 Herdr
session；清理前会重新验证该 session 的 plugin ownership，不会 attach、stop 或 delete 其他
session/workspace。输出 JSON 记录 worker kind、lane/pane identity、completion commit evidence、
latency、retry count、deterministic integration order、duplicate-lane count 和 cleanup readback。
模型输出具有非确定性，因此该 smoke 只提供人工/周期性证据，不能成为普通 PR 的单次硬门禁。


## License

[MIT](LICENSE)
