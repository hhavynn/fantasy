import type { Metadata } from "next";
import localFont from "next/font/local";
import { AccountMenu } from "@/components/AccountMenu";
import { CommandPalette } from "@/components/CommandPalette";
import { LeagueSwitcher } from "@/components/LeagueSwitcher";
import { BrandLink, BrandMark, MobileNav, NavGroups, SubNav, TabBar, WorkspaceRail, type RailTeams } from "@/components/Nav";
import { ProjectionSwitch } from "@/components/ProjectionSwitch";
import { ScopeRefresh } from "@/components/ScopeRefresh";
import { SyncPill } from "@/components/SyncPill";
import { TableScroll } from "@/components/TableScroll";
import { ThemeSwitch } from "@/components/ThemeSwitch";
import { api } from "@/lib/api";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { currentLeague } from "@/lib/league-scope.server";
import { NavVisibility } from "@/components/NavVisibility";
import { RAIL_INIT_SCRIPT } from "@/lib/navigation";
import { projectionSwitchVisible } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import { computeSyncSummary, type StatusPayload } from "@/lib/sync-status";
import type { TeamsPayload } from "@/lib/team-perspective";
import { THEME_INIT_SCRIPT } from "@/lib/theme";
import "./globals.css";

export const dynamic = "force-dynamic";

// Self-hosted (app/fonts, from @fontsource, see app/fonts/README.md) so `next build` never fetches Google Fonts.
// Same files Google served (latin, preloaded; Plex Sans Greek on demand for "Δ"), weights, `swap` and CSS variables
// as the old next/font/google setup. The metric-matched "… Fallback" faces live in globals.css.
const barlowCondensed = localFont({
  src: [
    { path: "./fonts/barlow-condensed-latin-500-normal.woff2", weight: "500", style: "normal" },
    { path: "./fonts/barlow-condensed-latin-600-normal.woff2", weight: "600", style: "normal" },
  ],
  display: "swap",
  fallback: ["Barlow Condensed Fallback"],
  adjustFontFallback: false,
  variable: "--font-barlow-condensed",
});
const plexSans = localFont({
  src: [{ path: "./fonts/ibm-plex-sans-latin-wght-normal.woff2", weight: "400 700", style: "normal" }],
  display: "swap",
  adjustFontFallback: false,
  variable: "--font-plex-sans",
});
const plexSansGreek = localFont({
  src: [{ path: "./fonts/ibm-plex-sans-greek-wght-normal.woff2", weight: "400 700", style: "normal" }],
  declarations: [{ prop: "unicode-range", value: "U+0370-0377,U+037A-037F,U+0384-038A,U+038C,U+038E-03A1,U+03A3-03FF" }],
  display: "swap",
  preload: false,
  adjustFontFallback: false,
  variable: "--font-plex-sans-greek",
});
const plexMono = localFont({
  src: [
    { path: "./fonts/ibm-plex-mono-latin-400-normal.woff2", weight: "400", style: "normal" },
    { path: "./fonts/ibm-plex-mono-latin-500-normal.woff2", weight: "500", style: "normal" },
  ],
  display: "swap",
  fallback: ["IBM Plex Mono Fallback"],
  adjustFontFallback: false,
  variable: "--font-plex-mono",
});

export const metadata: Metadata = {
  title: { default: "Fantasy Intel", template: "%s · Fantasy Intel" },
  description: "Fantasy football war room for your league",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const league = await currentLeague();
  // With the classic site retired an unscoped page (/leagues, /login) has no single-league routes to ask.
  const classicRetired = league === null && process.env.CLASSIC_SITE_ENABLED === "0";
  const [status, projection, profile, me, teamsRes] = await Promise.all([
    classicRetired ? null : api<NonNullable<StatusPayload>>("/api/status"), getProjectionSource(), getLeagueProfile(),
    league ? api<{ is_platform_admin?: boolean }>("/v2/me") : null,
    // The desktop rail's team picker: the same scoped list the pages' "Analyzing" picker reads.
    classicRetired ? null : api<TeamsPayload>("/api/teams")]);
  // A league page hides the owner research from everyone but platform admins (the API answers them 404 anyway);
  // the classic site is the owner's own.
  const hideOwnerOnly = league !== null && !(me?.ok && me.data.is_platform_admin === true);
  const s = status?.data;
  // A status read the backend answered from the league's previous files (lib/api.ts `stale`) marks the pill stale.
  const degraded = status?.ok === true && status.stale === true;
  const sync = computeSyncSummary(s ?? null, undefined, profile.provider, degraded);
  const teams: RailTeams | null = teamsRes?.ok && teamsRes.data.teams.length > 0
    ? {
      league: league?.id ?? null,
      defaultId: teamsRes.data.default_team_id,
      teams: teamsRes.data.teams.map((t) => ({ id: t.team_id, name: t.name, isDefault: t.is_owner_default })),
    }
    : null;
  const week = s?.league?.current_week ?? (teamsRes?.ok ? teamsRes.data.league.current_week : null) ?? null;
  // A league whose provider has no projections (Sleeper) is always on Ours: no switch to show. An unscoped page on the
  // retired classic site (/leagues, /terms, /account) has no league, so neither the switch nor a sync state.
  const showSwitch = !classicRetired && projectionSwitchVisible(profile);
  return (
    // suppressHydrationWarning: the inline script below sets data-theme on <html> before React hydrates.
    <html lang="en" className={`${barlowCondensed.variable} ${plexSans.variable} ${plexSansGreek.variable} ${plexMono.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: `${THEME_INIT_SCRIPT};${RAIL_INIT_SCRIPT}` }} />
      </head>
      <body>
        <NavVisibility hideOwnerOnly={hideOwnerOnly}>
        <header className="appbar">
          <div className="topbar">
            <BrandLink>
              <BrandMark />
              <span className="brand-word">Fantasy<span>Intel</span></span>
            </BrandLink>
            <NavGroups />
            <div className="topbar-end">
              {league ? <LeagueSwitcher current={league} /> : s?.league && (
                <span className="league-chip">
                  {s.league.name} <span className="faint">· wk {s.league.current_week}</span>
                </span>
              )}
              <CommandPalette />
              {showSwitch && <ProjectionSwitch value={projection} className="topbar-proj" />}
              <ThemeSwitch className="topbar-theme" />
              {classicRetired ? null : <SyncPill status={s ?? null} provider={profile.provider} degraded={degraded} />}
              <AccountMenu />
              <MobileNav projection={showSwitch ? projection : undefined} sync={classicRetired ? undefined : { state: sync.state, text: sync.text }} />
            </div>
          </div>
        </header>
        <SubNav />
        <ScopeRefresh />
        {/* Wide desktops: the workflow rail beside the page; below the rail breakpoint the shell is just main. */}
        <div className="shell">
          <WorkspaceRail teams={teams} week={week} />
          <main>{children}</main>
        </div>
        <TabBar />
        <TableScroll />
        </NavVisibility>
      </body>
    </html>
  );
}
