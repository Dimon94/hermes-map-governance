# Hermes Map Governance

Hermes Map Governance 是一个独立安装的 Hermes 治理插件。它把 Hermes 作为长期负责一个 Map 的 CEO，把董事长审批、CEO 决策、PM 提问和验收证据保留在治理平面，同时让 implementation tickets、worker lanes、Git 和 CI 继续承担执行真相。

项目目标是提供：

- Hermes Desktop 中独立的 Maps / 董事会页面；
- 每个 Map 唯一、长期且可恢复的 Hermes CEO 会话；
- CEO 授权边界与董事长重大决策审批；
- Hermes PM 对既有 `delivery-pipeline` 与 Herdr 的编排；
- GitHub Issue 单一治理真相源；
- 与 Hermes 核心 Kanban 执行状态机隔离的独立插件架构。

当前状态：已批准产品 Spec，implementation ticket graph 正在通过 GitHub Issues 发布和交付。

## 安装目标

插件最终通过 `~/.hermes/plugins/map-governance/` 或 Python package entry point 安装，不要求把产品代码合入 Hermes 核心仓库。

## License

[MIT](LICENSE)
