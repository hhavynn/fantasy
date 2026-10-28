# Deliberate UX regression sentinels (#202)

The normal UX acceptance matrix is documented in [release review](release-review.md). The tiny `fantasy-intel/frontend/e2e/ux-sentinels.spec.ts` tests the failure behaviour of two shared checks, not a production page and not a screenshot baseline.

- At an explicitly set **320px** viewport, a synthetic 500px-wide child must make `expectNoSidewaysScroll` reject; shrinking it to 100% must pass.
- A synthetic main landmark showing raw `NaN` and `undefined` must make `expectNoBrokenValues` reject; using missing-value dashes must pass.

These use the actual assertions imported from `e2e/ux.ts`, with no provider connection, user data, external services or new CI jobs. They are not comprehensive proof of the whole accessibility/route matrix; the existing real-route tests, reviewed visual baselines and independent QA remain required.
