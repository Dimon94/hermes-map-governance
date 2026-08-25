# tools

L2 module map。规则见 repo://docs/agents/layer-contracts.md。

## 模块职责

- 拥有：仓库治理自动化脚本。
- 不拥有：产品业务、`plugin/map_governance/` runtime、GitHub tracker product adapter、CI 编排。

## 成员清单

- `github-api.sh`：Agent/automation 调用 GitHub REST/GraphQL 的唯一 repository seam。
- `github-api.test.sh`：使用 fake `gh` 锁定 repository/path/argument forwarding，不访问网络。

## 生成规则

脚本直接运行，无 build artifact 入库。

## 反模式

- 在脚本中沉淀 product governance policy。
- 读取或打印 token。
- 绕过 task authority 执行 GitHub write。
- 与 `plugin/map_governance/tracker.py` 形成第二套产品 tracker adapter。

## 测试锚点

```bash
bash -n tools/github-api.sh tools/github-api.test.sh
bash tools/github-api.test.sh
```
