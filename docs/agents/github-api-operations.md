# GitHub API 操作指南

本文记录 Agent 与仓库自动化读取本项目 Issue、PR 和 Actions 的统一链路。产品运行时的 tracker adapter 不由本文拥有。

## 认证

首选 GitHub CLI 自己的凭据链路：

```bash
gh auth status --hostname github.com
tools/github-api.sh auth-check
```

脚本不读取、打印或转存 token。缺少认证时使用 `gh auth login` 恢复。不要把 `GH_TOKEN`、`GITHUB_TOKEN` 或 OAuth token 写入仓库、命令输出或 Issue。

## 固定坐标

- Repository：`Dimon94/hermes-map-governance`
- Web：`https://github.com/Dimon94/hermes-map-governance`
- REST repository prefix：`repos/Dimon94/hermes-map-governance`

测试或 fork 可用 `GITHUB_REPOSITORY` 显式覆盖。不要从当前 shell 用户名猜 repository owner。

## 统一入口

```bash
tools/github-api.sh repo GET ''
tools/github-api.sh issue 10
tools/github-api.sh pr 25
tools/github-api.sh runs
tools/github-api.sh raw GET rate_limit
```

`repo METHOD PATH` 自动加 repository prefix；`raw` 接受完整 API path。额外参数原样传给 `gh api`，例如：

```bash
tools/github-api.sh repo GET 'issues?state=open&per_page=100'
tools/github-api.sh repo POST 'issues/10/comments' -f body='verified result'
tools/github-api.sh raw POST graphql -f query='query { viewer { login } }'
```

只有任务明确授权 external write 时才使用 POST/PATCH/PUT/DELETE。写入后必须再次 GET，断言 stable identity、payload 与 state，而不是只看 HTTP success。

## Issue 与依赖

读取 ticket 时至少检查 `number`、`state`、`labels`、`body` 与 comments。阻断关系以 body 中 `Blocked by:` 为 canonical contract，详情见 repo://docs/agents/issue-tracker.md。

## Actions 排障

```bash
tools/github-api.sh runs
tools/github-api.sh run RUN_ID
gh run view RUN_ID --repo Dimon94/hermes-map-governance --log-failed
```

Actions status 只是编排证据。结论仍需结合 job log、artifact 与真实业务检查。

## 验证

```bash
bash -n tools/github-api.sh tools/github-api.test.sh
bash tools/github-api.test.sh
```

测试只使用 fake `gh`，不访问 GitHub，不读取真实凭据。
