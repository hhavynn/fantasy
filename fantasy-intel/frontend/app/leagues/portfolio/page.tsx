import Link from "@/components/LeagueLink";
import { connection } from "next/server";
import { Badge, EmptyState, Method, Notice, PageHeader, Pos, ProvenanceBadge, Section, SortableTh, Stat, StatStrip } from "@/components/ui";
import { apiRequest } from "@/lib/api";
import { authConfig } from "@/lib/auth-config";
import { Missing } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { dataAsOf } from "@/lib/league-switcher";
import { leagueName, multileagueUiEnabled } from "@/lib/my-leagues";
import { plural } from "@/lib/plural";
import {
  ACTION_CENTER_HREF, DEFAULT_SORT, EXCLUDED_TEXT, IDENTITY_TEXT, PORTFOLIO_PATH, POSITIONS, SORT_KEYS, exposureHref, filterHref,
  filterPlayers, heaviestByeWeek, injuryWord, leagueHref, leagueStatus, myLeagueHref, parseFilters, parsePortfolio, portfolioProblem, sortValue,
  type Exposure, type Portfolio, type PortfolioFilters, type PortfolioLeague, type PortfolioPlayer, type PortfolioWeek,
} from "@/lib/portfolio";
import { signalsOf } from "@/lib/signal-provenance";
import { parseSort, sortRows, type SearchParams } from "@/lib/sort";
import { currentUser } from "@/lib/supabase/server";
import styles from "./portfolio.module.css";

export const metadata = { title: "Player exposure" };

const TITLE = "Player exposure";
const META = "Which players you hold across your leagues, how many of your teams each one is on, and which weeks stack up byes.";

export default async function PortfolioPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  // Per request: the flag and the signed-in user decide everything here, so never prerender.
  await connection();
  const header = <PageHeader eyebrow="Connections" title={TITLE} meta={META}
    actions={<Link href={ACTION_CENTER_HREF} prefetch={false}>This week&apos;s decisions</Link>} />;
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) {
    return (
      <div className="stack">
        {header}
        <Notice tone="info" title="Connecting leagues is switched off in this deployment">
          Player exposure combines the leagues you add on the connections page, which this deployment does not offer.
        </Notice>
      </div>
    );
  }
  const sp = await searchParams;
  const user = authConfig(process.env) ? await currentUser() : null;
  if (!user) {
    return (
      <div className="stack">
        {header}
        <Notice tone="info" title="Sign in to see your players across leagues">
          This page reads only the leagues you follow and the team you saved in each, so it needs you signed in.{" "}
          <Link href={`/login?next=${encodeURIComponent(PORTFOLIO_PATH)}`} prefetch={false}>Sign in</Link>
        </Notice>
      </div>
    );
  }
  const res = await apiRequest<unknown>("/v2/portfolio");
  if (!res.ok) {
    const problem = portfolioProblem(res);
    return (
      <div className="stack">
        {header}
        <Notice tone={problem.tone} title={problem.title}>
          {problem.body}{" "}
          {/* A full reload: the page is rendered per request, so the leagues are read again. */}
          <a href={PORTFOLIO_PATH}>Try again</a>
        </Notice>
      </div>
    );
  }
  const data = parsePortfolio(res.data);
  if (!data) {
    return (
      <div className="stack">
        {header}
        <Notice tone="caution" title="Your player exposure could not be read">
          The backend answered in a format this page does not know. Each league&apos;s own pages still work.
        </Notice>
      </div>
    );
  }
  return (
    <div className="stack">
      {header}
      <Body data={data} sp={sp} />
    </div>
  );
}

function Body({ data, sp }: { data: Portfolio; sp: SearchParams }) {
  if (data.leagues.length === 0) {
    return (
      <EmptyState title="No leagues to combine yet"
                  action={<Link href="/leagues" prefetch={false}>Add a league</Link>}>
        Add a league and save your team in it; every league you manage a team in then counts here (leagues you only follow are listed, not counted).
      </EmptyState>
    );
  }
  const ready = data.coverage.leaguesReady;
  const shared = data.players.filter((p) => p.leagues >= 2).length;
  const top = data.players[0] ?? null;
  const heavy = heaviestByeWeek(data.weeks);
  const notCounted = data.leagues.filter((l) => l.state !== "ready");
  return (
    <>
      <StatStrip>
        <Stat label="Leagues counted" value={`${ready} of ${data.leagues.length}`}
              note={data.season ? `Season ${data.season}` : undefined} />
        <Stat label="Players" value={data.coverage.players} note={`${shared} on two or more of your teams`} />
        <Stat label="Most shared" value={top && ready > 0 ? `${top.leagues} of ${top.of}` : <Missing why="No roster was read" />}
              note={top?.name ?? undefined} />
        <Stat label="Heaviest bye week" value={heavy ? `Week ${heavy.week}` : <Missing why="No bye ahead in the loaded schedules" />}
              note={heavy ? `${heavy.bye.players} ${plural(heavy.bye.players, "player")} on bye` : undefined} />
      </StatStrip>
      {notCounted.length > 0 ? (
        <Notice tone="caution" title={`${notCounted.length} ${plural(notCounted.length, "league")} not counted`}>
          Their players are missing from every number here, so a count below can be lower than your real exposure. Each is listed
          under Your leagues with the reason.
        </Notice>
      ) : null}
      <PlayersSection data={data} sp={sp} />
      <WeeksSection weeks={data.weeks} />
      <LeaguesSection data={data} />
      <Method title="How it's counted">
        <p>
          Each league counts the team you saved in it, as of that league&apos;s latest synced data; no other team&apos;s roster is
          read. Only your newest season&apos;s leagues are combined, at most {data.limit} at once.
        </p>
        <p>
          A player is the same across leagues only on id evidence: a verified NFL (GSIS) id from the league&apos;s id map, a team
          defense&apos;s NFL team, or, between ESPN leagues, ESPN&apos;s own player id. A name match is never enough. A player whose ids
          disagree, or whose NFL id is reached from two different player ids, is not joined on that id and may appear on more than one row.
        </p>
        <p>
          Bye weeks come from the NFL schedule and count only when the team&apos;s whole regular-season schedule is loaded. Byes count
          active players (not IR or taxi) in leagues whose season still includes that week. Injury designations are each league&apos;s
          own, counted only in the week its data is from and never carried forward. A missing projection, bye or schedule is shown as —,
          never as 0. Nothing here is a risk score.
        </p>
      </Method>
    </>
  );
}

// ---- players --------------------------------------------------------------------------------------------------------

function PlayersSection({ data, sp }: { data: Portfolio; sp: SearchParams }) {
  const filters = parseFilters(sp);
  const sort = parseSort(sp, SORT_KEYS, DEFAULT_SORT);
  const rows = sortRows(filterPlayers(data.players, filters), (p) => sortValue(p, sort.key), sort.dir);
  const active = filters.q !== "" || filters.pos !== null || filters.shared;
  return (
    <Section title="Players" id="players" flush
             subtitle={`${data.coverage.players} ${plural(data.coverage.players, "player")} on ${data.coverage.exposures} roster ${plural(data.coverage.exposures, "spot")}. Open a row's leagues for each league's details.`}>
      <Controls filters={filters} sp={sp} active={active} />
      {rows.length === 0 ? (
        <div className={styles.pad}>
          <EmptyState title={data.players.length === 0 ? "No rostered players read" : "No player matches"}>
            {data.players.length === 0 ? "None of your leagues could be read with a saved team yet." : "Change the search or the filters."}
          </EmptyState>
        </div>
      ) : (
        <div className="table-wrap">
          <table className={styles.players}>
            <thead>
              <tr>
                <SortableTh sortKey="name" label="Player" current={sort} searchParams={sp} />
                <SortableTh sortKey="pos" label="Pos" current={sort} searchParams={sp} />
                <SortableTh sortKey="leagues" label="Leagues" current={sort} searchParams={sp} align="right"
                            title="Your leagues with him on your team, of the leagues counted" />
                <SortableTh sortKey="starters" label="Starting" current={sort} searchParams={sp} align="right"
                            title="Leagues where he is in your current lineup" />
                <SortableTh sortKey="bye" label="Bye" current={sort} searchParams={sp} align="right" defaultDir="asc" />
                <SortableTh sortKey="injury" label="Injury" current={sort} searchParams={sp} align="right"
                            title="Leagues whose data lists an injury designation" />
                <SortableTh sortKey="projections" label="Proj." current={sort} searchParams={sp} align="right"
                            title="Leagues with a projection this week, of his leagues" />
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => <PlayerRow key={p.key} p={p} />)}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function Controls({ filters, sp, active }: { filters: PortfolioFilters; sp: SearchParams; active: boolean }) {
  const keep = Object.entries(sp).flatMap(([k, v]) => (k === "q" || v === undefined ? [] : (Array.isArray(v) ? v : [v]).map((x) => [k, x] as const)));
  const posHref = (pos: string | null) => filterHref(sp, { pos });
  return (
    <div className={styles.controls}>
      <nav className="filters" aria-label="Position">
        <Link className="chip" href={posHref(null)} aria-current={filters.pos === null ? "page" : undefined} scroll={false}>All</Link>
        {POSITIONS.map((pos) => (
          <Link key={pos} className="chip" href={posHref(pos)} aria-current={filters.pos === pos ? "page" : undefined} scroll={false}>{pos}</Link>
        ))}
        <Link className="chip" href={filterHref(sp, { shared: filters.shared ? null : "1" })} aria-current={filters.shared ? "true" : undefined}
              scroll={false}>On 2+ teams</Link>
        {active ? <Link className="chip reset" href={filterHref(sp, { q: null, pos: null, shared: null })} scroll={false}>Clear</Link> : null}
      </nav>
      <form className={styles.search} role="search">
        {keep.map(([k, v], i) => <input key={`${k}-${i}`} type="hidden" name={k} value={v} />)}
        <label className={styles.searchLabel} htmlFor="portfolio-q">Search players, teams or leagues</label>
        <input id="portfolio-q" className={styles.input} type="search" name="q" defaultValue={filters.q} maxLength={60}
               placeholder="Player, NFL team or league" autoComplete="off" />
        <button type="submit">Search</button>
      </form>
    </div>
  );
}

function PlayerRow({ p }: { p: PortfolioPlayer }) {
  const id = IDENTITY_TEXT[p.identity.status];
  return (
    <tr>
      <td className={styles.name}>
        <span className={styles.playerName} title={p.name ?? undefined}>{p.name ?? <Missing why="No name in the league data" />}</span>
        <span className={styles.sub}>
          {p.nflTeam ?? "No NFL team"}{" · "}
          <Badge value={id.label} tone={id.tone} title={p.identity.reason ?? id.detail} />
        </span>
      </td>
      <td><Pos value={p.position} /></td>
      <td className="num">
        <details className={styles.drill}>
          <summary><strong>{p.leagues}</strong> of {p.of}<span className={styles.teams}> · {p.teams} {plural(p.teams, "team")}</span></summary>
          <ul className={styles.exposures}>
            {p.exposures.map((e) => <ExposureItem key={`${e.leagueId}-${e.playerId}`} e={e} />)}
          </ul>
        </details>
      </td>
      <td className="num">{p.starters}</td>
      <td className="num">{p.byeWeek ?? <Missing why={p.byeReason ?? "Bye week unknown"} />}</td>
      <td className="num">
        {p.injury.designated > 0
          ? <span title={p.injury.statuses.map(injuryWord).join(", ")}>{p.injury.designated} <span className={styles.faint}>{p.injury.statuses.map(injuryWord).join(", ")}</span></span>
          : "0"}
      </td>
      <td className="num">
        {p.projections.known === 0
          ? <Missing why="No league has a projection for him this week" />
          : `${p.projections.known} of ${p.projections.known + p.projections.missing}`}
      </td>
    </tr>
  );
}

function ExposureItem({ e }: { e: Exposure }) {
  const facts = [
    e.teamName ?? "Your team",
    e.reserve ? (e.slot === "TAXI" ? "Taxi" : "IR") : e.isStarter ? "Starting" : "Bench",
    e.injuryStatus && e.injuryStatus !== "ACTIVE" && e.injuryStatus !== "NORMAL" ? injuryWord(e.injuryStatus) : null,
    e.week !== null ? `Week ${e.week}` : null,
  ].filter(Boolean).join(" · ");
  return (
    <li>
      <Link href={exposureHref(e)} prefetch={false} title={e.leagueName ?? undefined}>{e.leagueName ?? "League"}</Link>
      <span className={styles.faint}> {providerName(e.provider)}</span>
      <span className={styles.expFacts}>{facts}</span>
      <span className={styles.expFacts}>
        Projection: {e.projection !== null
          ? `${e.projection.toFixed(1)} (${e.projectionOrigin === "model" ? "Ours" : providerName(e.provider)})`
          : <Missing why="No projection for him in this league this week" />}
        {e.stale ? " · not fresh" : ""}
        {e.syncedAt ? ` · ${dataAsOf(e.syncedAt, "UTC")} UTC` : ""}
      </span>
    </li>
  );
}

// ---- weeks ----------------------------------------------------------------------------------------------------------

function WeeksSection({ weeks }: { weeks: PortfolioWeek[] }) {
  return (
    <Section title="Byes and injuries by week" id="weeks" flush
             subtitle="Active players on bye in the leagues still playing that week; injury designations only in the week each league's data is from.">
      {weeks.length === 0 ? (
        <div className={styles.pad}><EmptyState title="No weeks to show">No league with a saved team could be read.</EmptyState></div>
      ) : (
        <div className="table-wrap">
          <table className={styles.weeks}>
            <thead>
              <tr>
                <th scope="col">Week</th>
                <th scope="col" className="num">On bye</th>
                <th scope="col" className="num" title="Of them, in your current lineup">Starting</th>
                <th scope="col" className="num" title="Leagues with a player on bye, of the leagues still playing">Leagues</th>
                <th scope="col" title="Players on bye">Who</th>
                <th scope="col" className="num" title="Roster spots with an injury designation, in leagues whose data is from this week">Injury</th>
              </tr>
            </thead>
            <tbody>
              {weeks.map((w) => (
                <tr key={w.week}>
                  <td>Week {w.week}</td>
                  <td className="num">
                    {w.bye.players}
                    {w.bye.exposures !== w.bye.players ? <span className={styles.faint}> ({w.bye.exposures} spots)</span> : null}
                    {w.bye.unknown > 0 ? <span className={styles.faint} title="Players whose bye week is unknown"> +{w.bye.unknown} unknown</span> : null}
                  </td>
                  <td className="num">{w.bye.starters}</td>
                  <td className="num">{w.bye.leagues} of {w.leaguesPlaying}</td>
                  <td className={styles.who}>
                    {w.bye.names.length ? w.bye.names.join(", ") : "None"}
                    {w.bye.players > w.bye.names.length ? ` and ${w.bye.players - w.bye.names.length} more` : ""}
                  </td>
                  <td className="num">
                    {w.injuries === null
                      ? <Missing why="No league's data is from this week, so no injury designation is counted" />
                      : <>{w.injuries.exposures}<span className={styles.faint}> in {w.injuries.leaguesReporting} {plural(w.injuries.leaguesReporting, "league")}</span></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ---- leagues --------------------------------------------------------------------------------------------------------

function LeaguesSection({ data }: { data: Portfolio }) {
  return (
    <Section title="Your leagues" id="leagues"
             subtitle="What each league added to this page and how old its data is."
             actions={<Link href={ACTION_CENTER_HREF} prefetch={false}>Decisions across leagues</Link>}>
      <ul className={styles.leagues}>
        {data.leagues.map((l) => <LeagueItem key={l.league.id} l={l} />)}
      </ul>
      {data.excluded.length > 0 ? (
        <>
          <p className={styles.faint}>Not combined: {data.excluded.length} {plural(data.excluded.length, "league")}.</p>
          <ul className={styles.excluded}>
            {data.excluded.map((x) => (
              <li key={x.league.id}>
                <Link href={myLeagueHref(x.league)} prefetch={false}>{leagueName(x.league)}</Link>
                <span className={styles.faint}> Season {x.league.season}. {EXCLUDED_TEXT[x.reason]}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </Section>
  );
}

function LeagueItem({ l }: { l: PortfolioLeague }) {
  const status = leagueStatus(l);
  const name = leagueName(l.league);
  const signals = signalsOf(l.provenance);
  const meta = [
    providerName(l.league.provider),
    l.team?.name ?? null,
    l.week !== null ? `Week ${l.week}` : null,
    l.league.role === null ? "Following" : null,
  ].filter(Boolean).join(" · ");
  return (
    <li className={styles.league} data-state={l.state}>
      <div className={styles.leagueHead}>
        <Link href={leagueHref(l)} prefetch={false} title={name} className={styles.leagueName}>{name}</Link>
        <Badge value={status.label} tone={status.tone} title={status.detail} />
      </div>
      <p className={styles.faint}>{meta}</p>
      {l.state === "ready" ? (
        <>
          <p className={styles.faint}>
            {l.rosterSize ?? 0} rostered · projections for {l.projection?.known ?? 0} of {l.projection?.total ?? 0}
            {l.projection?.source && l.projection.source !== "Unavailable" ? ` (${l.projection.source})` : ""}
            {l.syncedAt ? ` · ${dataAsOf(l.syncedAt, "UTC")} UTC` : " · Data time unknown"}
          </p>
          {l.stale ? <p className={styles.reason}>{status.detail}</p> : null}
          {signals.length > 0 ? <ProvenanceBadge signals={signals} /> : <p className={styles.faint}>Evidence age unknown</p>}
        </>
      ) : <p className={styles.reason}>{status.detail}</p>}
    </li>
  );
}
