# Current System Map

Status: active delivery

本文件描述当前可验证的系统边界、模块所有权、依赖方向和验证入口。目标能力以 repo://docs/specs/hermes-map-governance-spec.md 为准，未落地部分不得写成现状。

## 当前事实

- 仓库：`hermes-map-governance`，GitHub 坐标 `Dimon94/hermes-map-governance`。
- 产品边界：外部 standalone Hermes plugin；不修改 Hermes core。
- 技术栈：Python 3.11+、SQLite、FastAPI/Pydantic dashboard adapter、静态 JavaScript dashboard contribution、pytest 与 Node probe。
- 安装入口：`plugin.yaml`、根 `__init__.py`、`dashboard/manifest.json`。
- 公开操作入口：Hermes native `maps` capability、Dashboard plugin API、`map-governance-ceo` 与 `map-governance-pm` named toolset。
- 持久状态：GitHub Map Issue 拥有治理事实；profile-scoped `plugin-data/map-governance/registry.db` 拥有 binding、enforcement、Outbox 与 projection cache。
- 外部系统：GitHub Project/Issue、Hermes plugin/session/profile/dashboard contracts；真实 Herdr delivery runtime 尚未交付。
- 交付状态：主 checkout `main` 当前只有产品 bootstrap 文档与本仓开发模板，尚未包含 feature implementation；已验证的 `feature/map-1` 集成分支在 `e4eb9e7` 包含 #3-#10，其中 #10 durable Outbox 的 execution commit 通过 192 tests 与双轴 review。后续票仍由 delivery coordinator 编排；feature 合入 main 后必须更新本行。

## 顶层链路

    Native CLI / Dashboard / CEO Tool / PM Tool
        -> request-scoped profile/session identity
        -> MapGovernanceApplication
        -> domain policy + PluginStorage / Outbox
        -> GitHubTrackerAdapter / Hermes session seam / coordinator seam
        -> downstream readback and local projection

只读董事会链路：

    GitHub Map Issue + plugin bindings -> refresh -> rebuildable projection -> Maps dashboard

## 模块地图

| Module | Owns | Does not own | Public interface | Depends on | Verification |
| --- | --- | --- | --- | --- | --- |
| `map_governance/application.py` | 治理 use case、授权与跨模块编排 | Transport、Hermes core | `MapGovernanceApplication` | Policy、storage、tracker、session、Outbox seams | `tests/test_application.py` 及 feature-focused tests |
| `map_governance/stages.py`, `approvals.py`, `reports.py` | 确定性 stage、approval、report 合同 | 外部 I/O | 领域类型和 policy functions | 标准库 | 对应 `tests/test_*` |
| `map_governance/storage.py` | Plugin SQLite schema、binding、ledger、projection、process lease | GitHub truth、Kanban DB | `PluginStorage` | `sqlite3`, filesystem lock | storage/application/integration tests |
| `map_governance/outbox.py`, `effects.py` | Durable intent、claim、lease、retry、attempt、repair 与 effect adapter | 业务 authority、物理 exactly-once | Outbox repository/dispatcher/runtime | PluginStorage、external seams | `tests/test_outbox.py` + fault injection |
| `map_governance/tracker.py` | GitHub Project/Issue 协议翻译与 readback marker | 治理 policy | `TrackerAdapter`, `GitHubTrackerAdapter` | authenticated `gh` CLI | `tests/test_github_tracker.py` |
| `map_governance/sessions.py` | Canonical CEO session identity、adopt/mint/resume | Map truth、worker session | `CEOSessionRunner` | Hermes session contract | `tests/test_ceo_sessions.py`, `test_hermes_session_adapter.py` |
| `map_governance/native.py`, `dashboard/plugin_api.py`, tool adapters | CLI/HTTP/model tool 参数与错误翻译 | 业务规则和 canonical writes | Hermes plugin contracts | Application seam | native/dashboard/tool contract tests |
| `dashboard/dist/index.js` | Maps 页面 rendering 与交互 | Governance state ownership | Dashboard contribution | Plugin API | Node syntax + `tests/dashboard_shell_probe.mjs` |

依赖方向固定为：

    adapter -> application -> owning domain/storage module -> external seam

Adapter 不得绕过 application 直接裁决 stage、approval、session authority 或 Outbox outcome。

## 根不变量

- GitHub Map Issue 是治理 Source of Truth；projection 可删除重建。
- Plugin storage 与 `<PROFILE_HOME>/kanban.db` 隔离。
- 一个 Map 一个 canonical CEO Session；ambiguity fail closed，不猜测或删除。
- Request-scoped identity 授权；ambient process identity 不能跨 profile/Map 授权。
- External effect 先有 durable intent，再调用外部边界；每次重试先 readback。
- Executive board 不保存 implementation lane、worktree、commit 或 worker log。
- Local delivery 与 remote publication authority 分离。

## 验证

代码合入当前 checkout 后，稳定的最小入口为：

```bash
HERMES_AGENT_ROOT=/path/to/hermes-agent python3 -m pytest -q
uvx ruff@0.15.10 check __init__.py map_governance dashboard tests
uvx ruff@0.15.10 format --check __init__.py map_governance dashboard tests
uvx ty@0.0.21 check map_governance dashboard
python3 -m compileall -q __init__.py map_governance dashboard tests
node --check dashboard/dist/index.js
node --check tests/dashboard_shell_probe.mjs
```

Integration test 必须使用临时 `HERMES_HOME`。Python runtime dependency 目前由目标 Hermes checkout 提供；独立开发依赖声明与 CI 全量测试环境仍为 Unknown。

## PROTOCOL

根合同变化时同步：

- repo://CONTEXT.md。
- 根 repo://AGENTS.md。
- 命中 module 的 L2-L6 合同。
- 难回退取舍新建 ADR，不改写 accepted 历史。
