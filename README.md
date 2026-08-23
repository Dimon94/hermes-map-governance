# Hermes Map Governance

Hermes Map Governance 是一个独立安装的 Hermes 治理插件。它把 Hermes 作为长期负责一个 Map 的 CEO，把董事长审批、CEO 决策、PM 提问和验收证据保留在治理平面，同时让 implementation tickets、worker lanes、Git 和 CI 继续承担执行真相。

项目目标是提供：

- Hermes Desktop 中独立的 Maps / 董事会页面；
- 每个 Map 唯一、长期且可恢复的 Hermes CEO 会话；
- CEO 授权边界与董事长重大决策审批；
- Hermes PM 对既有 `delivery-pipeline` 与 Herdr 的编排；
- GitHub Issue 单一治理真相源；
- 与 Hermes 核心 Kanban 执行状态机隔离的独立插件架构。

当前交付包含最小可安装插件壳：Hermes native plugin、Maps dashboard 页面、
共享应用接口、独立存储命名空间和 readiness 诊断。Map 绑定及治理流程由后续票交付。

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

## 架构壳

- `MapGovernanceApplication` 是 REST、dashboard 和诊断入口共同调用的应用 seam。
- `hermes maps health` 与 `/api/plugins/map-governance/health` 返回同一 readiness。
- `/api/plugins/map-governance/board` 提供当前空 board 投影；dashboard 只负责呈现。
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
