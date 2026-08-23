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
until a GitHub Map Issue is bound by a later Map Governance capability.
