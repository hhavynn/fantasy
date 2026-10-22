import Link from "next/link";
import type { ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { TeamLink } from "@/components/TeamLink";
import { Badge, EmptyState, Glossary, Method, PageHeader, Section, SortableTh } from "@/components/ui";
import { api } from "@/lib/api";
import { Missing } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { plural } from "@/lib/plural";
import type { SortState } from "@/lib/sort";
import {
  afterText, backendPath, countsText, filterSummary, hasFilters, KIND_LABEL, KIND_TONE, legVerb, managerHref, parseTxQuery, POSITIONS,
  sortParams, tradeSides, txHref, type TransactionsFeed, type TxEvent, type TxLeg, type TxQuery, type TxTeam,
} from "@/lib/transactions";
import { LocalTime } from "./LocalTime";
import styles from "./transactions.module.css";

export const metadata = { title: "Transactions" };

// League transactions: every executed add, drop and trade the league sync stored, one row per move (a trade is one
// row however the provider recorded it). Filters live in the URL so a view can be bookmarked. Every number comes
// from the backend; nothing is graded or projected here.

export default async function TransactionsPage({ searchParams }: PageProps<"/transactions">) {
  const q = parseTxQuery(await searchParams);
  const [res, profile] = await Promise.all([api<TransactionsFeed>(backendPath(q)), getLeagueProfile()]);
  const prov = providerName(profile.provider);
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow="League" title="Transactions" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
        {(res.status === 400 || res.status === 404) && (
          <p><Link href={txHref(q, { manager: null, cursor: null })}>Show the newest moves</Link></p>
        )}
      </div>
    );
  }
  return <Body d={res.data} q={q} prov={prov} />;
}

function Body({ d, q, prov }: { d: TransactionsFeed; q: TxQuery; prov: string }) {
  const s = d.summary;
  const filtered = hasFilters(q);
  return (
    <div className="stack">
      <PageHeader
        eyebrow="League"
        title="Transactions"
        meta={<>
          {d.league.name} {d.league.season} · <b>{s.total}</b> {plural(s.total, "move")} stored
          {filtered && <> · <b>{s.matching}</b> {plural(s.matching, "matches", "match")} the filters</>}
        </>}
      />
      <p className={styles.lead}>
        {s.matching === 0
          ? (filtered ? "No stored move matches these filters." : `No executed moves stored yet. They appear after the next ${prov} sync.`)
          : <>{filtered ? "Matching" : "This season"}: {countsText(s.by_type)}.</>}
      </p>

      <Filters d={d} q={q} />

      <Section
        title={<>Moves <span className={styles.count}>{s.matching} {plural(s.matching, "move")}</span></>}
        flush={d.events.length > 0}
        footnote={d.events.length > 0 ? <>Times in your time zone (stored in UTC). {d.notes.trades}</> : undefined}
      >
        {d.events.length === 0
          ? <EmptyState title={filtered ? "No matching moves" : "No moves yet"}>
              {filtered
                ? <>Try fewer filters, or <Link href={txHref(q, { manager: null, type: null, pos: null, week: null })}>show every move</Link>.</>
                : <>Adds, drops and trades appear here after the next {prov} sync.</>}
            </EmptyState>
          : <MovesList d={d} q={q} prov={prov} />}
      </Section>

      <Pager d={d} q={q} />

      <Glossary terms={["FAAB", "—"]} />
      <Method title="How the feed is built">
        <ul className={styles.method}>
          <li>{d.notes.scope}</li>
          <li>{d.notes.trades}</li>
          <li>{d.notes.dates}</li>
          <li>{d.notes.bids}</li>
          <li>{d.notes.after}</li>
          {s.unplaced_week > 0 && (
            <li>
              {s.unplaced_week} {plural(s.unplaced_week, "move has", "moves have")} no stored week (seen only in the league activity
              feed), so a week filter leaves {s.unplaced_week === 1 ? "it" : "them"} out.
            </li>
          )}
        </ul>
      </Method>
    </div>
  );
}

// --------------------------------------------------------------------- filters --

function Chip({ href, active, children }: { href: string; active: boolean; children: ReactNode }) {
  return (
    <Link className={`chip ${active ? "active" : ""}`} aria-current={active ? "page" : undefined} href={href} scroll={false}>
      {children}
    </Link>
  );
}

function Filters({ d, q }: { d: TransactionsFeed; q: TxQuery }) {
  const o = d.options;
  return (
    <details className="filter-box">
      <summary>
        <span className="filter-box-title">Filters</span>
        <span className="filter-box-summary">{filterSummary(q, o.managers)}</span>
      </summary>
      <div className={styles.chipRows}>
        <nav className="filters" aria-label="Move type">
          <span className={styles.chipLabel}>Type</span>
          <Chip href={txHref(q, { type: null })} active={q.type === null}>All</Chip>
          {o.types.map((k) => <Chip key={k} href={txHref(q, { type: k })} active={q.type === k}>{KIND_LABEL[k]}</Chip>)}
        </nav>
        <nav className="filters" aria-label="Manager">
          <span className={styles.chipLabel}>Manager</span>
          <Chip href={txHref(q, { manager: null })} active={q.manager === null}>All</Chip>
          {o.managers.map((m) => (
            <Chip key={m.team_id} href={txHref(q, { manager: m.team_id })} active={q.manager === m.team_id}>
              <span className={styles.chipName} title={m.name ?? undefined}>{m.name ?? `Team ${m.team_id}`}</span>
            </Chip>
          ))}
        </nav>
        <nav className="filters" aria-label="Player position">
          <span className={styles.chipLabel}>Position</span>
          <Chip href={txHref(q, { pos: null })} active={q.pos === null}>All</Chip>
          {POSITIONS.filter((p) => o.positions.includes(p)).map((p) => (
            <Chip key={p} href={txHref(q, { pos: p })} active={q.pos === p}>{p}</Chip>
          ))}
        </nav>
        {o.weeks.length > 0 && (
          <nav className="filters" aria-label="Week">
            <span className={styles.chipLabel}>Week</span>
            <Chip href={txHref(q, { week: null })} active={q.week === null}>All</Chip>
            {o.weeks.map((w) => <Chip key={w} href={txHref(q, { week: w })} active={q.week === w}>{w}</Chip>)}
          </nav>
        )}
        {hasFilters(q) && (
          <p className={styles.reset}>
            <span>{filterSummary(q, o.managers)}</span>
            <Link className="chip reset" href={txHref(q, { manager: null, type: null, pos: null, week: null })} scroll={false}>Reset filters</Link>
          </p>
        )}
      </div>
    </details>
  );
}

// ------------------------------------------------------------------------ table --

function MovesList({ d, q, prov }: { d: TransactionsFeed; q: TxQuery; prov: string }) {
  const current: SortState = { key: q.sort, dir: q.dir };
  const sp = sortParams(q);
  return (
    <div className="table-wrap">
      <table className={styles.table}>
        <thead>
          <tr>
            <SortableTh sortKey="date" label="When" current={current} searchParams={sp} defaultDir="desc" />
            <SortableTh sortKey="week" label="Wk" current={current} searchParams={sp} align="right" defaultDir="asc" />
            <SortableTh sortKey="type" label="Type" current={current} searchParams={sp} />
            <th scope="col">Teams</th>
            <th scope="col">Players</th>
          </tr>
        </thead>
        <tbody>
          {d.events.map((e) => <Row key={e.event_id} e={e} prov={prov} />)}
        </tbody>
      </table>
    </div>
  );
}

function TeamName({ team }: { team: TxTeam | null }) {
  if (!team) return <Missing why="Team not stored" />;
  const name = team.name ?? `Team ${team.team_id}`;
  return <TeamLink href={managerHref(team.team_id)} className={styles.team} title={name}>{name}</TeamLink>;
}

function Row({ e, prov }: { e: TxEvent; prov: string }) {
  const when = e.processed_at ?? e.proposed_at;
  return (
    <tr>
      <td className={styles.when}>
        {when ? <LocalTime iso={when} /> : <Missing why={`No timestamp from ${prov}`} />}
        {e.processed_at && e.proposed_at && <span className={styles.sub}>Proposed <LocalTime iso={e.proposed_at} /></span>}
        {!e.processed_at && e.proposed_at && <span className={styles.sub}>Proposed time; no processed time stored</span>}
      </td>
      <td className="num">{e.week ?? <Missing why="No week stored: seen only in the league activity feed" />}</td>
      <td>
        <Badge value={KIND_LABEL[e.kind]} tone={KIND_TONE[e.kind]} />
        {e.bid_amount !== null && <span className={styles.sub}>Bid ${e.bid_amount}</span>}
      </td>
      <td className={styles.teams}>
        {e.teams.length === 0
          ? <Missing why="No team stored for this move" />
          : e.teams.map((t, i) => <span key={t?.team_id ?? i} className={styles.teamLine}><TeamName team={t} /></span>)}
      </td>
      <td className={styles.players}>
        <Players e={e} prov={prov} />
      </td>
    </tr>
  );
}

function LegLine({ leg, showVerb = true }: { leg: TxLeg; showVerb?: boolean }) {
  const after = afterText(leg.after);
  const verb = leg.action === "trade" && leg.from_team?.name ? `From ${leg.from_team.name}` : legVerb(leg);
  return (
    <li className={styles.leg}>
      {leg.espn_player_id !== null
        ? <PlayerIdentity id={leg.espn_player_id} name={leg.full_name} position={leg.position} team={leg.pro_team}
            meta={showVerb ? <span className={leg.action === "drop" ? styles.dropVerb : styles.verb}>{verb}</span> : undefined} />
        : <Missing why="Player not stored" />}
      {after && <span className={styles.after}>{after}</span>}
    </li>
  );
}

function Players({ e, prov }: { e: TxEvent; prov: string }) {
  const unlisted = e.unlisted_assets.length > 0 && (
    <p className={styles.sub}>Also moved {e.unlisted_assets.join(" and ")}, which {prov} stores but this feed does not list.</p>
  );
  if (e.legs.length === 0) {
    return <>
      <span className={styles.sub}>Players not recorded by {prov} for this {e.kind === "trade" ? "trade" : "move"}.</span>
      {unlisted}
    </>;
  }
  if (e.kind !== "trade") {
    return <ul className={styles.legs}>{e.legs.map((leg, i) => <LegLine key={`${leg.action}-${leg.espn_player_id}-${i}`} leg={leg} />)}</ul>;
  }
  return (
    <>
      {tradeSides(e).map((side, i) => (
        <div key={side.team?.team_id ?? `rest-${i}`} className={styles.side}>
          <p className={styles.sideHead}>{side.team ? <><TeamName team={side.team} /> received</> : "Also in this trade"}</p>
          <ul className={styles.legs}>
            {side.legs.map((leg, j) => <LegLine key={`${leg.espn_player_id}-${j}`} leg={leg} showVerb={side.team === null || leg.action === "trade"} />)}
          </ul>
        </div>
      ))}
      {unlisted}
    </>
  );
}

// ------------------------------------------------------------------------ pager --

function Pager({ d, q }: { d: TransactionsFeed; q: TxQuery }) {
  const p = d.page;
  if (p.total === 0 || (!p.has_prev && !p.next_cursor)) return null;
  return (
    <nav className={styles.pager} aria-label="Transaction pages">
      {p.has_prev && <Link className="chip" href={txHref(q, { cursor: p.prev_cursor })}>Previous</Link>}
      <span className="sub">{`${p.offset + 1}–${p.offset + p.returned} of ${p.total}`}</span>
      {p.next_cursor && <Link className="chip" href={txHref(q, { cursor: p.next_cursor })}>Next</Link>}
    </nav>
  );
}
