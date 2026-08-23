# 分支与 Worktree 纪律

主词：**主目录只保留 main；一张票一棵树一名写者；集成权归 coordinator。**

## 分支纪律

1. 主目录只 checkout `main`。无交付编排的单会话改动可直接在主目录工作，不为普通修改预建分支。
2. Wayfinder/交付编排为一张 Map 建一个 integration worktree；每个 implementation ticket 使用独立 Codex App 或 Herdr worktree 与 task branch。
3. 一棵 worktree 同一时刻只有一名写者。并行研究默认只读；需要写入时使用自己的 ticket worktree。
4. Task branch 必须从 coordinator 给定的 exact base 创建。禁止从另一 task branch 再开分支。
5. Integration branch 只接收单票、已验证、可审计的 commit。只有 owning coordinator 能 cherry-pick/rebase/merge、更新 lane registry 和清理 delivery worktree。
6. 所有权、路径或 Git common-dir 不明确时立即停止写入。先用 `git worktree list --porcelain`、branch、HEAD 和 registry 证明坐标。
7. 不删除仍有活动 task、未提交差异或未集成 commit 的 branch/worktree。收口只清理当前票明确拥有的坐标。

## 普通单会话流程

1. 在主目录确认 branch 为 `main`，记录 HEAD 与 dirty paths。
2. 保留用户已有的无关修改，只编辑本需求路径。
3. 运行 focused check 与 `git diff --check`。
4. 只有用户明确要求时才 stage、commit、push 或创建 PR。

## 编排任务流程

1. Coordinator 从 Issue graph 计算 ready frontier，并记录 integration worktree、branch 与 exact base。
2. Ticket worker 在独立 worktree 核验 common-dir、cwd、branch、base ancestry 与 clean state，随后才获得写权。
3. Worker 按 repo://docs/agents/development-workflow.md 实现、验证与双轴 review，形成一张票一个候选 commit。
4. Worker 把 terminal status、commit、checks、dirty state 与 blocker 回报 coordinator，不自行修改 integration tree 或 tracker closeout。
5. Coordinator 在 integration worktree 读回 commit、切换 lane 状态、集成并重跑 focused check；失败则回到原 ticket worker 修复。
6. 票已集成且证据写回 tracker 后，coordinator 才移除 execution worktree 与 task branch。
7. Map 全部验收完成后，coordinator 按明确授权把 integration branch 收口到 main、publication 或 PR；local integration 不自动获得 remote publication 权限。

## Worktree 环境合同

- Worktree 只共享 Git object database。可写 runtime state、日志、数据库、PID、build output 与临时 Hermes home 必须留在本树或显式临时目录。
- 依赖目录只有在版本完全相同且工具支持时才可只读复用。修改 lockfile/dependency 时使用本树独立环境。
- `.env`、credential 与 profile state 不复制进仓库。测试使用显式变量和临时 `HERMES_HOME`。
- Build、真实请求、提交与清理前都执行存活门：`.git` 可解析，branch/path/HEAD 仍出现在 `git worktree list --porcelain`，cwd 指向预期树。
- 真实请求前核验 process cwd、PID、port、branch/HEAD 与 artifact time，避免命中另一 checkout。

## 检查

- 主目录不在 main：停止并盘点。
- 一个 task branch 没有对应 worktree/票据：停止并盘点。
- 同一 worktree 有多个写者：停止并移交写权。
- Source checkout 或 integration path 被迁移：从 Git common-dir 与 registry 重新解析，确认 commit/branch 完整后再继续。
- 单票任务不执行全局 `git worktree prune`，不清理其他 task 的资源。
