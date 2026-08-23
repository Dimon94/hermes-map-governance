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
PM contract 固定 `dispatch_runtime: herdr`，但 Herdr commissioning 与 worker dispatch
transport 仍由后续票交付；本票只预留 bounded coordinator bridge。
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

CEO Tool 只提供 `inspect`、`record_decision` 与 `request_approval`。Profile、session 与
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

PM assignment 固定绑定 request-scoped profile/session 与一个 Map，不读取 process env。
受控 coordinator application seam 以 `idle → active → idle` 运行；tracker-confirmed report
或确认的 dispatch 结束后必须 idle，失败的 report 保持同一 active turn 供同 payload 重试，
重启后可安全 resume。acceptance outcome evidence 可出现在 Map detail，但 worker checks、
commands 与 lane activity 仍只属于 delivery artifacts。该 seam 不创建 implementation card，也不保存 pane、
worktree、lane 或 worker log。

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
  canonical session open routes 是同一应用接口的薄适配器。
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


## License

[MIT](LICENSE)
