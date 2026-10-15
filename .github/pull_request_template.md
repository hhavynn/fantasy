## Summary of changes

<!-- Brief, high-level summary of what this PR does and why. -->

## How the change was tested

<!-- Specific commands run, test results, and local verification steps. -->

## Known limitations

<!-- Any edge cases, unsupported scenarios, or remaining follow-ups. -->

## Data & schema implications

<!-- Note any DuckDB schema changes, table recreations, migrations, or confirm none. -->

## Concurrent branch & dependency considerations

<!-- Note active workstreams (e.g. Trade Intelligence V3, Waiver Intelligence V3) or package/lockfile changes. -->

## Hygiene checklist

- [ ] No real credentials, tokens, or cookies (`.env`, `ESPN_S2`, `SWID`) committed
- [ ] No DuckDB database files or WAL files (`*.duckdb`, `*.duckdb.wal`) committed
- [ ] No raw archived ESPN payloads or licensed external utilization datasets committed
- [ ] CI checks pass locally (`uv run pytest` in backend, `npm run lint` & `npm run build` in frontend)
