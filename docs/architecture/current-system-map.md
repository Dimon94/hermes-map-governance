# Current System Map

Status: active delivery

本文件描述当前可验证的系统边界、模块所有权、依赖方向和验证入口。目标能力以 repo://docs/specs/hermes-map-governance-spec.md 为准，未落地部分不得写成现状。

## 当前事实

- 仓库：`hermes-map-governance`，GitHub 坐标 `Dimon94/hermes-map-governance`。
- 产品边界：外部 standalone Hermes plugin；不修改 Hermes core，不复用 Kanban 状态机。
- 技术栈：Python 3.11+、SQLite、FastAPI/Pydantic dashboard adapter、静态 JavaScript dashboard contribution、pytest 与 Node probe。
- 仓库边界：仓库根拥有 prompts、governance docs、tests、development tools 与 CI；它不是安装 payload。
- Package 边界：`plugin/` 是 runtime code、manifest、after-install、dashboard 与 Skills 的唯一 canonical physical home。没有 copied runtime tree 或 escaping symlink。
- 公开安装标识符：`https://github.com/Dimon94/hermes-map-governance.git#plugin`。Hermes 只扫描并安装该 repository subdirectory。
- 安装入口：`plugin/plugin.yaml`、`plugin/__init__.py`、`plugin/dashboard/manifest.json`。
- 公开操作入口：Hermes native `maps` capability、Dashboard plugin API、`map-governance-ceo` 与 `map-governance-pm` named toolset。
- 持久状态：GitHub Map Issue 拥有治理事实；profile-scoped `plugin-data/map-governance/registry.db` 拥有 binding、enforcement、Outbox 与 projection cache。
- 外部系统：GitHub Project/Issue、Hermes plugin/session/profile/dashboard contracts、plugin-owned Herdr session，以及 credential-separated publisher seam。
- 交付状态：#3-#19 已进入当前系统。已交付 Map binding/stage/board、canonical CEO Session、决策与审批、PM report、durable Outbox、live/stale、setup/doctor、Herdr commissioning、decision round trip、Codex/Claude lane routing、restart repair、publication closeout 与只读 portfolio。生产部署、兼容矩阵与发布渠道仍为 Unknown。

## 顶层链路

    Native CLI / Dashboard / CEO Tool / PM Tool
        -> request-scoped profile/session identity
        -> MapGovernanceApplication
        -> domain policy + PluginStorage / Outbox
        -> GitHubTrackerAdapter / Hermes session seam / CoordinatorRuntime
        -> governed publisher seam when explicit authority exists
        -> downstream readback and local projection

只读董事会链路：

    GitHub Map Issue + plugin bindings -> refresh -> rebuildable projection -> Maps dashboard

## 模块地图

| Module | Owns | Does not own | Public interface | Depends on | Verification |
| --- | --- | --- | --- | --- | --- |
| `plugin/map_governance/application.py` | 治理 use case、授权与跨模块编排 | Transport、Hermes core | `MapGovernanceApplication` | Policy、storage、tracker、session、Outbox、coordinator 与 publisher seams | `tests/test_application.py` 及 feature-focused tests |
| `plugin/map_governance/stages.py`, `approvals.py`, `reports.py` | 确定性 stage、approval、report 合同 | 外部 I/O | 领域类型和 policy functions | 标准库 | 对应 `tests/test_*` |
| `plugin/map_governance/storage.py` | Plugin SQLite schema、binding、ledger、projection、process lease | GitHub truth、Kanban DB | `PluginStorage` | `sqlite3`, filesystem lock | storage/application/integration tests |
| `plugin/map_governance/outbox.py`, `effects.py` | Durable intent、claim、lease、retry、attempt、repair 与 effect adapter | 业务 authority、物理 exactly-once | Outbox repository/dispatcher/runtime | PluginStorage、external seams | `tests/test_outbox.py` + fault injection |
| `plugin/map_governance/tracker.py` | GitHub Project/Issue 协议翻译与 readback marker | 治理 policy | `TrackerAdapter`, `GitHubTrackerAdapter` | authenticated `gh` CLI | `tests/test_github_tracker.py` |
| `plugin/map_governance/sessions.py` | Canonical CEO session identity、adopt/mint/resume | Map truth、worker session | `CEOSessionRunner` | Hermes session contract | `tests/test_ceo_sessions.py`, `test_hermes_session_adapter.py` |
| `plugin/map_governance/coordinator.py` | Plugin-owned Herdr root、delivery lane handoff 与 recovery | GitHub truth、worker implementation | `CoordinatorRuntime` | Herdr CLI、delivery registry seam | commissioning/coordinator/delivery tests |
| `plugin/map_governance/prerequisites*.py` | 显式 setup plan/apply 与纯读 doctor | Secret storage、隐式 remediation | Prerequisite application | Hermes profiles、configured executable seams | prerequisite tests |
| `plugin/map_governance/publication.py` | Worker/publisher authority boundary、handoff 与 remote evidence | PM authority、Map truth | Governed publication types/adapters | setup config、Git/GitHub seams | publication/publisher boundary tests |
| `plugin/map_governance/native.py`, `plugin/dashboard/plugin_api.py`, tool adapters | CLI/HTTP/model tool 参数与错误翻译 | 业务规则和 canonical writes | Hermes plugin contracts | Application seam | native/dashboard/tool contract tests |
| `plugin/dashboard/dist/index.js` | Maps 页面 rendering 与交互 | Governance state ownership | Dashboard contribution | Plugin API | Node syntax + `tests/dashboard_shell_probe.mjs` |

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
uvx ruff@0.15.10 check plugin tests
uvx ruff@0.15.10 format --check plugin tests
uvx ty@0.0.21 check \
  --python /path/to/hermes-agent/venv/bin/python \
  --extra-search-path plugin \
  --extra-search-path /path/to/hermes-agent \
  plugin/map_governance plugin/dashboard
python3 -m compileall -q plugin tests
node --check plugin/dashboard/dist/index.js
node --check tests/dashboard_shell_probe.mjs
node --check tests/dashboard_live_probe.mjs
bash -n tools/github-api.sh tools/github-api.test.sh
bash tools/github-api.test.sh
git diff --check
```

Integration test 必须使用临时 `HERMES_HOME`。Lifecycle test 显式保留默认安全扫描，安装本地 Git snapshot 的 `#plugin` 子目录，并断言 install metadata、discovery、Map open、dashboard/tool/skill、disable 与 remove。CI 从精确 Hermes commit 创建 checkout，使用 locked Python dependency environment，并从 upstream lockfile 构建 dashboard SPA 后运行同一完整门禁。

## PROTOCOL

根合同变化时同步：

- repo://CONTEXT.md。
- 根 repo://AGENTS.md。
- 命中 module 的 L2-L6 合同。
- 难回退取舍新建 ADR，不改写 accepted 历史。
