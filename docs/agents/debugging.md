# Debugging

用途：诊断错误、失败、性能回归、环境不一致和改动不生效。主词是证据闭环。

## 1. 锁定现象

记录 exact 输入、时间、环境、错误、状态和可观察输出。

优先读取 canonical source of truth。缓存、日志和页面状态只作辅助证据。

完成标准：一条具体命令、测试或请求可以复现现象。无法复现时，根因写 Unknown。

## 2. 排序证据

证据优先级：

1. 运行状态、持久数据和外部系统原始响应。
2. 当前源码与能执行的测试。
3. accepted 合同、ADR 和最近 AGENTS.md。
4. 当前官方文档。
5. 推理。

冲突时，记录被采用和被放弃的证据，并说明原因。

完成标准：证据顺序能解释观察到的现象。

## 3. 锁定执行身份

确认请求实际命中了当前工作区构建出的进程或服务。

检查：

- 端口和 PID。
- 进程命令和 cwd。
- 当前分支和 HEAD。
- 构建产物时间。
- 环境变量来源和配置文件。

完成标准：代码、构建产物、进程和请求入口指向同一 checkout。无法证明时停止修改结论。

## 4. 先红后修

用最小公开 seam 精确复现失败。然后修改共享 owner。

避免：

- 在 caller 加特例绕过 owner。
- 只改错误文案掩盖失败。
- 依赖重试、sleep 或缓存失效碰运气。
- 为测试公开私有 helper。

完成标准：同一检查修复前失败，修复后通过；相邻失败边界仍保持。

## 5. 证明根因

根因必须解释现象，并满足：

- 实体真实存在。
- 因果关系有证据。
- 原因足以产生结果。
- 没有未说明的必要附加原因。

相关而非因果的因素标为扩散风险，不标为根因。

完成标准：移除或控制该原因后，现象按预测变化。

## 6. 走真实入口

使用用户相同的公开 route、CLI、UI action 或任务入口。

同时断言：

- transport 结果。
- 业务状态和错误语义。
- 持久状态读回。
- 外部副作用次数。
- 身份、权限和输出归属。

完成标准：公开入口证明目标问题已消失，且没有非预期副作用。

## 7. 收口

运行 focused tests、模块测试、build 或语法检查，以及 git diff --check。

记录：

- 根因。
- 修复 owner。
- 复现与验证命令。
- 仍为 Unknown 的失败。
- 与当前 diff 无法建立因果关系的既有问题。

完成标准：结论可由另一位开发者复跑。

## 8. 缺陷落账（GitHub）

第 7 节的记录产物归本仓 GitHub Issue，不落一次性本地笔记：

- 可复现的缺陷先开 issue 再修；issue 描述写现象、复现命令、环境。无法复现的按第 1 节标 Unknown，挂 `needs-info`。
- 标签流转按 repo://docs/agents/triage-labels.md：`needs-triage` 进场，评估后转 `ready-for-agent` / `ready-for-human` / `wontfix`。
- 修复 PR 描述写 `Closes #<number>`；根因、复现与验证命令写进 issue 的 resolution comment 再关闭。
- Actions 失败排查用 repo://tools/github-api.sh 或 `gh run view --log-failed` 读取 run/job 证据，禁止凭 badge 或 webhook status 猜结论。

约定细节见 repo://docs/agents/issue-tracker.md。

完成标准：Issue、PR、根因记录三者互相可达。
