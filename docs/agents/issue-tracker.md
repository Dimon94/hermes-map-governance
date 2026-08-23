# Issue Tracker

本仓的 Issue、PRD、Wayfinder Map 与 implementation ticket 归 GitHub：

```text
https://github.com/Dimon94/hermes-map-governance
```

## 约定

- 建 Issue：默认走 GitHub Web UI；用户明确批准自动化时才用 API。
- 读 Issue：读取 description、labels、comments、state 与 close reason。
- 评论、关闭、打标：使用 GitHub Issue 操作，写后从 GitHub readback。
- Triage label 映射见 repo://docs/agents/triage-labels.md。
- PR 使用 `Closes #<number>` 关联票据；local commit、push、PR 和 merge 各自需要相应授权。

## Skill 术语

“publish to the issue tracker”表示在本仓 GitHub Issues 创建 Issue；“fetch the relevant ticket”表示按编号读取 Issue description、labels 与 comments。

## 阻断与依赖

Canonical 阻断语义写在 Issue body：`Blocked by: #x #y`。GitHub 原生关系只作导航。解除阻断时必须读取被引用 Issue 的 closed state，不能只检查 label 或 comment。

## Wayfinding 编排

- Map：顶层 Issue，label `wayfinder:map`。
- 子 ticket：label `wayfinder:map-<map_number>` 加类型 label，body 首行写 `Map: #<map_number>`。
- Frontier：筛选该 Map 的 open ticket，排除 `wayfinder:claimed` 与仍有 open blocker 的 ticket。
- 认领：开工前加 `wayfinder:claimed`；完成后写 resolution evidence、close ticket，并更新 Map decision/index。
- Delivery lane registry 可作为 Issue comment 中的可恢复坐标，但不进入产品 Executive Board。

## 程序化访问

仓库治理自动化统一走 repo://tools/github-api.sh。认证、endpoint、GraphQL 和写后验证见 repo://docs/agents/github-api-operations.md。
产品运行时不能依赖该脚本；产品 GitHub 协议归 `map_governance/tracker.py`。
