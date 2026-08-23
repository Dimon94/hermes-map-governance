# Map Governance installed

If the plugin was installed without `--enable`, activate it first:

```bash
hermes plugins enable map-governance
```

Verify all shell components and plugin-owned storage:

```bash
hermes maps health
```

Then start `hermes dashboard` and open **Maps**. An empty-state board is expected
until an existing GitHub Map Issue is bound:

```bash
hermes maps project configure \
  --url https://github.com/orgs/OWNER/projects/PROJECT_NUMBER
hermes maps bind \
  --project PROJECT_NODE_ID \
  --issue https://github.com/OWNER/REPOSITORY/issues/ISSUE_NUMBER
```

The commands use authenticated, read-only `gh api graphql` requests. Repeating a
bind is idempotent. Run `hermes maps refresh` to rebuild dashboard projections
from GitHub and the plugin-owned binding registry. The GitHub token needs the
`read:project` scope.
