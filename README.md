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
projection；CEO 会话与交付编排仍由后续票交付。

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
```

重复执行同一条 bind 命令是幂等的。`refresh` 从 GitHub tracker truth 与最小 binding
registry 重新生成缓存；删除 projection rows 不会丢失 Map 绑定。

开放 Map 的治理流程为：`discovery → awaiting-approval → authorized → delivery`；
`delivery` 可进入 whole-Map blocker 的 `decision` 或 `acceptance`，两者可返回
`delivery`。任一 active stage 可进入 `parked`，恢复时回到 `discovery` 重新评估。
`done` 与 `cancelled` 不通过 label transition 请求，而分别投影自 GitHub Issue 的
completed 与 not-planned 关闭原因。Dashboard 使用服务器返回的合法下一阶段按钮，
GitHub 写失败时保留旧卡片并显示错误。

## 架构壳

- `MapGovernanceApplication` 是 REST、dashboard 和诊断入口共同调用的应用 seam。
- `hermes maps health` 与 `/api/plugins/map-governance/health` 返回同一 readiness。
- `/api/plugins/map-governance/board` 提供按 GitHub Project 分组的 board 投影；
  `/projects`、`/bindings`、`/refresh` 与 `/transitions` POST routes 是同一应用接口的
  薄适配器。
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
