# Semantic and keyboard fail-closed gate sentinels (#202)

These tests supplement the overflow and raw numeric-value sentinels in [202-gate-sentinels.md](202-gate-sentinels.md).

The `frontend/e2e/ux-semantic-sentinels.spec.ts` spec synthesizes **no user/league data** and runs against the exact shared production release-check helpers:
- Render a header, five section nav links, and heading **without a main landmark**; `expectStructure` must reject. Replacing the wrapper with `main` must pass.
- Render a button lacking an accessible name; `expectAccessible` (axe WCAG gates) must reject. Adding an explicit name must pass.
- Render a meaningful action with `tabindex="-1"`; `tabThrough` must show it is unreachable by Tab. Restoring native focus order must provide a visible focused main-page action.

These are intentional test failures **caught by the assertion harness**; a correct CI run passes because the gate rejects each bad fixture and accepts its corrected version. This doesn't replace real-route, provider-authorization, or reviewed visual-baseline QA.
