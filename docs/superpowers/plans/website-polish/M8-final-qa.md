# M8 — Final QA & polish

Branch: `polish/m8-final-qa` · Ports: backend 8001 (shared) · frontend 3108
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M8-report.md` (outside the repo; never commit it)

## Goal

Take the site from "built" to "finished". You are the last line before the owner uses it
every week. Use the `impeccable` skill (audit/polish) if available.

## 1. Audit (write findings to the report first, then fix)

For every route (`lib/navigation.ts` pages + `/data`, `/players?id=`, `/managers/<id>`
tabs, `/matchup?week=`, 404) at 1440, 768 and 375 in dark and light:
alignment and spacing on the 4/8 grid, type hierarchy, consistent section headers and
footnotes, number formatting (tabular, consistent decimals per metric, signs on deltas),
empty/loading/error states, truncation of long player/team names (ellipsis + title),
focus order and visible focus, contrast, copy (sentence case, factual, no jargon without a
tooltip), console errors/warnings, hydration warnings.

## 2. Performance

- Pages that await several endpoints: wrap slower sections in `<Suspense>` with skeleton
  fallbacks so the page header and first section stream immediately.
- Check `npm run build` output for oversized client bundles; keep client islands small.
- No layout shift on load from fonts (use `next/font` settings as needed) or images.

## 3. Consistency sweep

- `grep -rn 'style={{' app components` → only geometry remains; list survivors in report.
- No raw hex colours outside token definitions (`grep -rnE '#[0-9a-fA-F]{3,8}' app components lib`).
- Remove dead CSS (selectors no longer referenced) from `globals.css` and modules.

## 4. Docs

- `fantasy-intel/README.md` dashboard pages table: add `/matchup`, mention ⌘K and themes.
- `fantasy-intel/frontend/PRODUCT.md`: final kit + principles.

## Acceptance

Global checks pass; report contains the audit table (route × issue × fix) with every issue
either fixed or explicitly justified; zero console errors on every route.
