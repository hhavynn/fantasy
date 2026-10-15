# Design Document: Fantasy Intelligence Header & UI Cleanup

- **Date:** 2026-10-14
- **Scope:** Frontend Global Header, Navigation, Sync Status & Responsive Design
- **Branch:** `gemini/ui-header-cleanup`

## 1. Overview and Problem Statement

The Fantasy Intelligence dashboard currently renders all feature routes as a flat list in `app/layout.tsx`. As routes have grown (Overview, Players, Waivers, Trades, Discover Trades, Schedule, Playoff Race, Team Value, Managers, Data Quality, and incoming Luck Lab), the header has become crowded, lacks visual hierarchy, and wraps awkwardly on laptop and mobile viewports. Furthermore:
1. Sync information displays verbose multi-line raw text consuming horizontal space.
2. Active navigation states are absent, making it difficult for managers to tell which section is currently active.
3. Mobile viewports have minimal responsive handling, causing header overflow or wrapping across several lines.

## 2. Architecture & Components

To preserve server-side fetching and minimize client-side bundle size, the header logic is split into three modular components:

### 2.1 `AppHeader.tsx` (Server Component)
- **Path:** `fantasy-intel/frontend/components/AppHeader.tsx`
- **Responsibility:**
  - Serves as the top-level container `<header className="topbar">`.
  - Displays the brand identity (`fantasyintel`).
  - Displays compact League Identity: League Name and Week badge (e.g. `Los Subies · Week 6`).
  - Renders `MainNavigation` and `SyncStatus`.

### 2.2 `MainNavigation.tsx` (Client Component)
- **Path:** `fantasy-intel/frontend/components/MainNavigation.tsx`
- **Responsibility:**
  - Route-aware navigation using `usePathname()` from `next/navigation`.
  - Desktop layout:
    - Primary direct links:
      - Overview (`/`)
      - My Team (`/my-team/value`)
      - Players (`/players`)
      - Waivers (`/waivers`)
      - Trades (`/trades`)
      - Trade Finder (`/trades/discover`)
    - Accessible dropdown menus:
      - **Insights ▾**: Schedule Intelligence (`/schedule-intelligence`), Playoff Race (`/playoff-race`), and Luck Lab (`/luck-lab`, disabled by default on `main` via config flag, easily toggled on for PR #15).
      - **More ▾**: Managers (`/managers`), Data Quality (`/data`).
  - Dropdown behavior:
    - Toggled via click or keyboard (`Enter` / `Space`).
    - Closes on `Escape` key and returns focus to the toggle button.
    - Closes on click outside.
    - ARIA attributes: `aria-expanded`, `aria-haspopup="true"`.
  - Active Route Highlighting:
    - Sets `aria-current="page"` on the exact active link.
    - Highlights active parent dropdown (Insights or More) when any subroute is active.
    - Route matching rules:
      - Exact match for `/`.
      - Nested prefix matching for subroutes (`/trades/discover` activates Trade Finder; `/trades` activates Trades; `/managers/*` activates Managers and More).
  - Mobile layout (<768px):
    - Hamburger button (`aria-label="Toggle navigation menu"`, `aria-expanded`).
    - Collapsible panel displaying primary routes followed by grouped Insights and More sections.
    - Touch targets with minimum 44px height.

### 2.3 `SyncStatus.tsx` (Client Component)
- **Path:** `fantasy-intel/frontend/components/SyncStatus.tsx`
- **Responsibility:**
  - Receives `SyncInfo` and `league` from server-fetched `/api/status`.
  - Compact trigger button/pill:
    - Healthy: neutral dot `● Sync status ▾`.
    - Warning: amber dot `● Sync warning ▾` if any recent run reported an error.
    - Offline: red dot `● Backend offline` if data is null/unreachable.
  - Expandable popover anchored beneath trigger:
    - ESPN last successful sync (`when(s.sync.last_success.espn)`).
    - NFL last successful sync (`when(s.sync.last_success.nfl)`) + stats coverage week.
    - Analytics last successful sync (`when(s.sync.last_success.analytics)`).
    - Recent errors list if any sync run failed.
  - Closes on click outside or `Escape`. Focus restored to button.

## 3. Styling & CSS Updates

### 3.1 `globals.css`
- **Header:**
  - Sticky `.topbar` with streamlined flex layout, consistent padding (`8px 16px`), and `z-index: 20`.
  - Clear visual separator and background `#0b0f14`.
- **Navigation Links & Dropdowns:**
  - Restrained dark-mode aesthetic with hover backgrounds (`var(--panel-2)`) and active accent pill/border (`var(--accent)`).
  - High-contrast keyboard `:focus-visible` styling (`outline: 2px solid var(--accent); outline-offset: 2px;`).
- **Popover & Dropdown Menus:**
  - Background `var(--panel)`, border `1px solid var(--border)`, border-radius `8px`, box-shadow `0 8px 24px rgba(0,0,0,0.5)`.
- **Responsive Navigation:**
  - Clean breakpoints at 1024px, 768px, and 390px/320px.
  - Drawer menu for mobile viewports without horizontal scroll or content clipping.
- **`.page-head` Polish:**
  - Mobile-friendly flex direction (column on `<640px`, row with baseline alignment on `≥640px`) to prevent subtitle and title overlap/misalignment.

## 4. Integration Safety & Non-Disruption
- No changes to backend code, APIs, or database models.
- Preserves `/my-team/value`, `/trades`, `/trades/discover`, and all other active routes.
- Accounts for `/luck-lab` in navigation data structures without activating dead links on `main`.
- Navigation remains fully operable even when backend is offline.

## 5. Verification Plan
- Unit & type checks: `npm ci`, `npm run lint`, `npm run typecheck`, `npm run build`.
- Responsive layout verification at 1440px, 1280px, 1024px, 768px, 390px, and 320px.
- Accessibility verification: keyboard tab navigation, `Escape` key dismiss, ARIA states.
- Offline status simulation.
