# Fantasy Intelligence Global Header & Navigation Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a polished, compact, and accessible responsive global header for Fantasy Intelligence with grouped navigation, route-aware active states, compact sync status indicator with details popover, and mobile navigation drawer.

**Architecture:** Split the global header into pure utility libraries (`navigation.ts`, `sync-status.ts`) with comprehensive unit tests, server component `AppHeader.tsx`, client components `MainNavigation.tsx` and `SyncStatus.tsx`, updated `layout.tsx`, and refined CSS in `globals.css` addressing `.topbar`, dropdowns, mobile drawer, `.page-head`, and `.grid-2` overflow.

**Tech Stack:** Next.js 16 (App Router), React 19, TypeScript, CSS (vanilla dark theme variables), Node.js native test runner (`node --test`).

## Global Constraints

- Frontend-only: no changes to backend, APIs, database, projections, or algorithms.
- Preserve all existing routes (`/`, `/players`, `/waivers`, `/trades`, `/trades/discover`, `/schedule-intelligence`, `/luck-lab`, `/playoff-race`, `/my-team/value`, `/managers`, `/data`).
- Only link to existing routes; structure navigation config so future routes like `/start-sit` can be added with a single configuration entry.
- Maintain existing dark theme, blue accent, typography, and table vocabulary.
- Keep server-side `/api/status` fetching in Server Component (`layout.tsx` / `AppHeader.tsx`).
- No large UI or icon libraries; native semantic HTML and CSS with SVG icons.
- Keyboard accessible (`Escape`, `Tab`, `Enter`, `Space`, visible focus outlines, outside click dismissal, `aria-current="page"`, `aria-expanded`).

---

### Task 1: Navigation Definition and Route Matching (`lib/navigation.ts` and `lib/navigation.test.ts`)

**Files:**
- Create: `fantasy-intel/frontend/lib/navigation.ts`
- Create: `fantasy-intel/frontend/lib/navigation.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export type NavItem = {
    label: string;
    href: string;
    exact?: boolean;
  };

  export type NavGroup = {
    id: "insights" | "more";
    label: string;
    items: NavItem[];
  };

  export const PRIMARY_NAV_ITEMS: NavItem[];
  export const NAV_GROUPS: NavGroup[];
  export const ALL_NAV_ITEMS: NavItem[];

  export function getActiveRoute(pathname: string): {
    activeHref: string | null;
    activeGroupId: "insights" | "more" | null;
  };
  ```

- [ ] **Step 1: Write the failing tests in `lib/navigation.test.ts`**
  Cover root route (`/`), nested trade routes (`/trades` vs `/trades/discover`), subroute matching (`/managers/123`), parent dropdown activation (`/luck-lab` activates `insights`), unknown route handling, and route list integrity.

- [ ] **Step 2: Run test to verify it fails**
  Run: `npm test` from `fantasy-intel/frontend`
  Expected: FAIL with "Cannot find module './navigation.ts'"

- [ ] **Step 3: Implement `lib/navigation.ts`**
  Implement `PRIMARY_NAV_ITEMS`, `NAV_GROUPS`, `ALL_NAV_ITEMS`, and `getActiveRoute(pathname)` with longest-prefix / boundary-aware matching logic.

- [ ] **Step 4: Run test to verify it passes**
  Run: `npm test` from `fantasy-intel/frontend`
  Expected: PASS

- [ ] **Step 5: Commit**
  Run: `git add lib/navigation.ts lib/navigation.test.ts && git commit -m "feat(nav): add navigation definition and route-matching helper with unit tests"`

---

### Task 2: Sync Status Classification Logic (`lib/sync-status.ts` and `lib/sync-status.test.ts`)

**Files:**
- Create: `fantasy-intel/frontend/lib/sync-status.ts`
- Create: `fantasy-intel/frontend/lib/sync-status.test.ts`

**Interfaces:**
- Consumes: `SyncInfo` and `League` types from `lib/types.ts`
- Produces:
  ```ts
  export type SyncHealthState = "healthy" | "warning" | "offline" | "not_synced";

  export type SyncSummary = {
    state: SyncHealthState;
    label: string;
    tone: "neutral" | "warn" | "bad";
    details: {
      espn: { timestamp: string | null; formatted: string };
      nfl: { timestamp: string | null; formatted: string; statsWeek: number | null };
      analytics: { timestamp: string | null; formatted: string };
    };
    activeFailure: { source: string; message: string | null; finished_at: string | null } | null;
  };

  export function computeSyncSummary(
    data: { sync: SyncInfo; nfl_latest_week: number | null; league: { name: string; current_week: number } | null } | null
  ): SyncSummary;
  ```

- [ ] **Step 1: Write the failing tests in `lib/sync-status.test.ts`**
  Cover:
  - Backend unreachable (`null` data) -> `offline`, tone `bad`, label "Backend offline"
  - Completely un-synced data -> `not_synced`, tone `warn`, label "Not synced"
  - Healthy sync runs -> `healthy`, tone `neutral`, label "Sync status"
  - Active failure in latest run without newer success -> `warning`, tone `warn`, label "Sync warning"
  - Resolved failure where a newer success occurred -> `healthy`, tone `neutral`
  - Partially populated status response

- [ ] **Step 2: Run test to verify it fails**
  Run: `npm test` from `fantasy-intel/frontend`
  Expected: FAIL with "Cannot find module './sync-status.ts'"

- [ ] **Step 3: Implement `lib/sync-status.ts`**
  Implement conservative health semantics distinguishing offline, un-synced, active failures, and healthy states using existing `when()` helper from `lib/format.tsx`.

- [ ] **Step 4: Run test to verify it passes**
  Run: `npm test` from `fantasy-intel/frontend`
  Expected: PASS

- [ ] **Step 5: Commit**
  Run: `git add lib/sync-status.ts lib/sync-status.test.ts && git commit -m "feat(sync): add sync-status classification logic with unit tests"`

---

### Task 3: Interactive Components — `SyncStatus.tsx` and `MainNavigation.tsx`

**Files:**
- Create: `fantasy-intel/frontend/components/SyncStatus.tsx`
- Create: `fantasy-intel/frontend/components/MainNavigation.tsx`

**Interfaces:**
- `SyncStatus`:
  - Client component (`"use client"`).
  - Props: `{ status: { sync: SyncInfo; nfl_latest_week: number | null; league: { name: string; current_week: number } | null } | null }`.
  - Accessible disclosure button + popover.
  - Keyboard handlers: `Escape` closes and restores focus, outside click closes.
- `MainNavigation`:
  - Client component (`"use client"`).
  - Uses `usePathname()` from `next/navigation` and `getActiveRoute()` from `lib/navigation.ts`.
  - Desktop: Primary links + `Insights ▾` and `More ▾` dropdowns.
  - Mobile: Hamburger toggle button + sliding/folding mobile navigation list.
  - Keyboard: `Escape` closes dropdown/mobile menu, outside click closes, `aria-expanded` and `aria-current="page"`. Closes automatically on navigation.

- [ ] **Step 1: Create `components/SyncStatus.tsx`**
  Build the compact status badge trigger and detailed popover with click-outside listener, keyboard escape listener, and accessible ARIA attributes (`aria-expanded`, `aria-controls`, `aria-haspopup="dialog"`).

- [ ] **Step 2: Create `components/MainNavigation.tsx`**
  Build the route-aware desktop and mobile navigation using `PRIMARY_NAV_ITEMS`, `NAV_GROUPS`, and `getActiveRoute(pathname)`. Add click-outside and `Escape` handlers that close any open dropdown and return focus to its trigger button.

- [ ] **Step 3: Verify types and linting**
  Run: `npm run lint && npm run typecheck`
  Expected: PASS

- [ ] **Step 4: Commit**
  Run: `git add components/SyncStatus.tsx components/MainNavigation.tsx && git commit -m "feat(ui): add interactive SyncStatus and MainNavigation components"`

---

### Task 4: Layout and Header Integration (`AppHeader.tsx`, `layout.tsx`)

**Files:**
- Create: `fantasy-intel/frontend/components/AppHeader.tsx`
- Modify: `fantasy-intel/frontend/app/layout.tsx`

**Interfaces:**
- `AppHeader`:
  - Server Component.
  - Props: `{ status: { league: { name: string; current_week: number } | null; sync: SyncInfo; nfl_latest_week: number | null } | null }`.
  - Renders `<header className="topbar">`, Brand logo, League/Week info with truncation tooltip, `MainNavigation`, and `SyncStatus`.
- `layout.tsx`:
  - Keeps server fetch: `const status = await api<Status>("/api/status");`.
  - Replaces inline navigation and verbose sync info with `<AppHeader status={status.data} />`.

- [ ] **Step 1: Implement `components/AppHeader.tsx`**
  Create clean header composition with brand, league title/badge, navigation, and sync status.

- [ ] **Step 2: Update `app/layout.tsx`**
  Replace legacy markup with `<AppHeader status={s} />`. Ensure layout remains clean, server-rendered, and error-tolerant when backend is offline.

- [ ] **Step 3: Verify build and types**
  Run: `npm run typecheck && npm run build`
  Expected: PASS

- [ ] **Step 4: Commit**
  Run: `git add components/AppHeader.tsx app/layout.tsx && git commit -m "refactor(layout): integrate AppHeader into root layout"`

---

### Task 5: CSS & Responsive Layout Polish (`globals.css`)

**Files:**
- Modify: `fantasy-intel/frontend/app/globals.css`

**Changes:**
- `.topbar`: Clean flexbox layout with sticky positioning, subtle border, and proper stacking context (`z-index: 20`).
- Brand & League Info: Compact typography, graceful text truncation with ellipsis for long league names (`max-width: 180px` on desktop, `max-width: 130px` on mobile).
- Navigation links:
  - Base: `color: var(--muted); padding: 5px 10px; border-radius: 6px;`
  - Hover: `color: var(--text); background: var(--panel-2);`
  - Active: `color: var(--accent); background: var(--info-bg); font-weight: 600;`
  - Dropdown trigger button: reset button styles, display arrow indicator `▾`, active state if child route active.
- Dropdowns & Popovers:
  - Absolute positioning, background `var(--panel)`, border `1px solid var(--border)`, shadow, `z-index: 50`.
  - High-contrast `:focus-visible` styling (`outline: 2px solid var(--accent); outline-offset: 2px;`).
- Mobile Menu:
  - Hamburger toggle button (`display: none` on desktop, visible on `<860px` or `<768px`).
  - Mobile menu panel expanding beneath header with minimum 44px tap targets.
- Fix `.page-head`:
  - Mobile flex-direction column with 4px gap, upgrading to row with baseline alignment on `min-width: 640px`.
- Fix `.grid-2`:
  - Change `minmax(420px, 1fr)` to `minmax(min(100%, 340px), 1fr)` so two-column cards don't cause horizontal overflow on mobile screens.

- [ ] **Step 1: Edit `app/globals.css`**
  Implement the styles for topbar, nav links, active indicators, dropdowns, popovers, mobile drawer, `.page-head`, and `.grid-2`.

- [ ] **Step 2: Run linter, typecheck, and build**
  Run: `npm run lint && npm run typecheck && npm run build`
  Expected: PASS

- [ ] **Step 3: Commit**
  Run: `git add app/globals.css && git commit -m "style: polish topbar, navigation dropdowns, mobile menu, and responsive layout"`

---

### Task 6: Comprehensive Verification & Visual Inspection Across Viewports

- [ ] **Step 1: Run complete test suite and build**
  Run:
  ```bash
  npm test
  npm run lint
  npm run typecheck
  npm run build
  ```
  Expected: 100% PASS with 0 errors.

- [ ] **Step 2: Start Next.js server and verify UI at required viewports**
  Viewports to test:
  - 1440px (Desktop wide)
  - 1280px (Desktop / Laptop)
  - 1024px (Small laptop / Tablet landscape)
  - 768px (Tablet portrait / Mobile collapse transition)
  - 390px (Standard mobile phone)
  - 320px (Narrow mobile phone)
  Verify:
  - Active state correctness on `/`, `/trades`, `/trades/discover`, `/managers/1`, `/my-team/value`, `/luck-lab`, unknown route.
  - Dropdown interactions (click, Enter/Space, Escape, outside click, focus return).
  - Sync status popover (healthy, warning, offline states).
  - Mobile hamburger toggle and drawer navigation.
  - No horizontal scrolling or header clipping on any viewport.
  - Capture visual screenshots.

- [ ] **Step 3: Commit any final refinements**
  Run: `git commit -am "chore: visual and responsive layout verification refinements"`

---

### Task 7: Pull Request Creation and Codex Review

- [ ] **Step 1: Push branch to `origin/gemini/ui-header-cleanup`**
- [ ] **Step 2: Open PR against `main`**
  Title: `UI: Compact responsive header, grouped navigation and sync status`
- [ ] **Step 3: Request Codex review and compile completion report**
