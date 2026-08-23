# Domain Documentation

本文件说明开发任务如何使用领域文档。

## 开始工作

先读：

- repo://CONTEXT.md。
- 目标文件最近的 AGENTS.md。
- 涉及当前区域的 accepted ADR。
- repo://docs/architecture/current-system-map.md。

缺失内容按 Unknown 处理。不要凭空补齐业务事实。

## 统一语言

issue、测试名、类型名、状态名和文档使用 CONTEXT.md 的 canonical term。

出现新概念时先判断：

1. 代码和运行状态是否已经表达该概念。
2. 需求或 accepted ADR 是否确认边界。
3. 它是否只是现有概念的同义词。

只有前两项提供证据且第三项为否时，才更新词表。

## ADR 冲突

若提案与 accepted ADR 冲突，明确指出冲突、证据和重开决定的理由。不要静默覆盖。

## 文档边界

- CONTEXT.md 负责统一语言。
- System Map 负责当前模块、所有权和依赖。
- ADR 负责难回退决定和取舍。
- AGENTS.md 负责特定目录的修改规则和验证入口。
- 代码与测试负责可执行行为。

同一规范性陈述只保留一个 canonical owner。其他文档用 repo:// 指针引用。
