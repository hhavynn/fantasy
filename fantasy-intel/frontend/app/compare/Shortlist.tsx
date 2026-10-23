import Link from "@/components/LeagueLink";
import type { ReactNode } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import {
  Badge, EmptyState, Glossary, Method, Notice, PageHeader, Section, Tabs, Verdict, VerdictFigure, VisuallyHidden,
} from "@/components/ui";
import { api } from "@/lib/api";
import {
  type CompareCard, type IdLike, type RowUnit, type SearchParamsLike, type ShortCell, type ShortlistPlayer,
  type ShortlistResponse, MAX_SHORTLIST, MIN_SHORTLIST, bestHeadline, byeText, compareHref, injuryCell,
  isBestProjected, isShortlistResponse, metricCell, nextOpponentText, nextWeekCell, pairFromShortlistHref,
  parseShortlistParams, playerName, projectionSourceLine, rosCell, rosterShort, rosterText, shortlistFromPair,
  shortlistHref, shortlistPairs, startPickNote, trendCell, unknownShortlistId, withoutPlayer, DASH,
} from "@/lib/compare";
import { cx } from "@/lib/cx";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import { isUnknownTeam, readTeamSelection, teamParamFrom, withTeamId } from "@/lib/team-perspective";
import { ShortlistPicker } from "./ShortlistPicker";
import styles from "./compare.module.css";

const TITLE = "Compare players";
const META = `Up to ${MAX_SHORTLIST} players for one lineup slot, from the projections, usage and injury reports the app already stores. Best projected is the highest stored projection, not a start/sit verdict.`;
/** Picking another team keeps the shortlist and slot; the start pick was made for the previous team. */
const TEAM_RESET = ["start"] as const;

/** "Two players" | "Shortlist": the pair view (unchanged URLs) and the shortlist, one page. */
export function CompareModes({ mode, a, b, ids }: { mode: "pair" | "shortlist"; a?: IdLike; b?: IdLike; ids?: readonly number[] }) {
  const pair = ids && ids.length > 0 ? compareHref(ids[0], ids[1]) : compareHref(a, b);
  const list = ids ? shortlistHref({ ids }) : shortlistFromPair(a, b);
  return (
    <Tabs
      label="Comparison view"
      current={mode === "pair" ? pair : list}
      items={[{ href: pair, label: "Two players" }, { href: list, label: `Shortlist (up to ${MAX_SHORTLIST})` }]}
    />
  );
}

type State = { ids: number[]; slot: string | null; start: number | null; team: string | null };

function Shell({ state, meta, actions, children }: { state: State; meta?: ReactNode; actions?: ReactNode; children?: ReactNode }) {
  return (
    <div className="stack">
      <PageHeader eyebrow="Players" title={TITLE} meta={meta ?? META} actions={actions}>
        <CompareModes mode="shortlist" ids={state.ids} />
        <ShortlistPicker ids={state.ids} slot={state.slot} start={state.start} team={state.team} />
      </PageHeader>
      {children}
    </div>
  );
}

export async function ShortlistView({ sp }: { sp: SearchParamsLike }) {
  const { ids, slot, start, errors } = parseShortlistParams(sp);
  const sel = readTeamSelection(sp);
  const team = teamParamFrom(sp);
  const state: State = { ids, slot, start, team };
  const href = (s: Partial<State>) => shortlistHref({ ...state, ...s });
  const [source, profile] = await Promise.all([getProjectionSource(), getLeagueProfile()]);
  const espnProjections = profile.providerProjections;
  const urlNotice = errors.length > 0 ? (
    <Notice tone="caution" title="Part of the link could not be read">
      <ul className={styles.errorList}>{errors.map((e) => <li key={e}>{e}</li>)}</ul>
    </Notice>
  ) : null;

  if (sel.kind === "invalid") {
    return (
      <Shell state={{ ...state, team: null }}>
        <UnknownTeam path={href({ team: null, start: null })} sel={sel} />
      </Shell>
    );
  }

  if (ids.length < MIN_SHORTLIST) {
    return (
      <Shell state={state} meta={`${META} ${projectionSourceLine(source === "ours" ? "independent" : "espn", espnProjections)}.`}>
        {urlNotice}
        <Section title={ids.length === 0 ? "Build a shortlist" : "Add another player"}>
          <EmptyState
            title={ids.length === 0 ? "No players on the shortlist yet" : "One player is on the shortlist"}
            action={ids.length === 1 ? <Link href={`/players?id=${ids[0]}`}>Open the selected player&apos;s page</Link> : <Link href="/start-sit">Open Start/Sit</Link>}
          >
            <p>Search above to add {ids.length === 0 ? `two to ${MAX_SHORTLIST} players` : "at least one more player"}: the flex or streaming candidates you are choosing between.</p>
            <p>Then pick the lineup slot they compete for. Any pair opens in the full two-player comparison.</p>
          </EmptyState>
        </Section>
      </Shell>
    );
  }

  const path = `/api/compare/shortlist?ids=${ids.join(",")}${slot ? `&slot=${encodeURIComponent(slot)}` : ""}`;
  const res = await api<ShortlistResponse>(withTeamId(withSource(path, source), sel));
  const teamPicker = (current: number | null) => (
    <TeamPerspective path="/compare" sp={sp} current={current} reset={TEAM_RESET} />
  );

  if (!res.ok) {
    if (isUnknownTeam(res, sel)) {
      return <Shell state={state}><UnknownTeam path={href({ team: null, start: null })} sel={sel} detail={res.error} /></Shell>;
    }
    if (res.status === 404) {
      const id = unknownShortlistId(res.error);
      return (
        <Shell state={state}>
          <Section>
            <EmptyState
              title="Player not found"
              action={id !== null ? <Link href={href({ ids: withoutPlayer(ids, id) })}>Remove player {id}</Link> : <Link href={shortlistHref({ ids: [] })}>Start over</Link>}
            >
              <p>{id !== null ? <>No player in this league&apos;s pool has the id {id}.</> : res.error} Remove it, then add a different player above.</p>
            </EmptyState>
          </Section>
        </Shell>
      );
    }
    if (res.status === 422) {
      return (
        <Shell state={state}>
          <Notice tone="caution" title="The shortlist could not be built">
            {res.error} {slot ? <Link href={href({ slot: null })}>Show every slot</Link> : null}
          </Notice>
        </Shell>
      );
    }
    return <Shell state={state}><BackendProblem error={res.error} kind={res.kind} status={res.status} /></Shell>;
  }
  if (!isShortlistResponse(res.data)) {
    return (
      <Shell state={state}>
        <Notice tone="negative" title="Unexpected response">
          The backend answered, but not with a shortlist this page can read. Reload; if it persists, the backend and frontend may be out of step.
        </Notice>
      </Shell>
    );
  }

  const r = res.data;
  const best = r.best_projected;
  // Named only when some player has a number from it (#276 / D-14): otherwise "Unavailable", never ESPN or Ours.
  const src = best.source_label;
  const named = src !== "Unavailable";
  const teamName = r.team?.name ?? null;
  const meta = (
    <>
      {projectionSourceLine(r.source, espnProjections)} · week {r.as_of_week} projections · usage through week {r.completed_through_week}
      {" "}· scoring: {r.scoring.label}.
    </>
  );
  const pick = startPickNote(r, start);
  const bestPlayer = best.leaders.length === 1 ? r.players.find((p) => p.card.player.espn_player_id === best.leaders[0]) : null;

  return (
    <Shell state={state} meta={meta} actions={teamPicker(r.team?.team_id ?? null)}>
      {urlNotice}
      <Verdict
        tone={best.status === "none" || best.status === "incomplete" ? "caution" : "info"}
        eyebrow={`Week ${best.week} · ${slot ? `${slot} slot` : "any slot"} · ${named ? `${src} projections` : "no projections"}`}
        headline={bestHeadline(r)}
        figures={<>
          <VerdictFigure label={named ? `Best projected (${src})` : "Best projected"} value={best.value === null ? DASH : best.value.toFixed(1)} note={bestPlayer ? playerName(bestPlayer.card) : best.leaders.length > 1 ? "Tied" : "None"} emphasis />
          <VerdictFigure label="Gap to the next player" value={best.gap === null ? DASH : best.gap.toFixed(1)} note={best.gap === null ? "No other player to compare" : "Projected points"} />
          <VerdictFigure label="Players compared" value={`${best.considered.length} of ${r.players.length}`} note={best.excluded.length ? `${best.excluded.length} left out, reasons below` : "All of them"} />
        </>}
      >
        <p>{best.text}</p>
        {pick ? <p><strong>Start pick:</strong> {pick}</p> : <p>Mark the player you will start below. That is your call; the page only shows the numbers.</p>}
        {best.excluded.length > 0 ? (
          <ul className={styles.excluded}>{best.excluded.map((e) => <li key={e.espn_player_id}>{e.text}</li>)}</ul>
        ) : null}
      </Verdict>

      <SlotPicker r={r} state={state} />

      <Section
        title="Shortlist"
        subtitle={`${projectionSourceLine(r.source, espnProjections)}, in this league's scoring (${r.scoring.label}). N/A: the measure does not apply to the position. —: missing, never zero.`}
        flush
      >
        <ShortlistGrid r={r} start={start} espnProjections={espnProjections} />
      </Section>

      <Section title="Players" subtitle={`Roster status for ${teamName ?? "the selected team"}. Mark one start pick; remove a player to make room for another.`}>
        <ul className={styles.shortPlayers}>
          {r.players.map((p, i) => (
            <PlayerRow key={p.card.player.espn_player_id} p={p} i={i} r={r} state={state} teamName={teamName} />
          ))}
        </ul>
      </Section>

      <Section title="Open a pair in the full comparison" subtitle="Projection, usage, schedule and charts for two players side by side.">
        <ul className={styles.pairs}>
          {shortlistPairs(r.players).map(([x, y]) => {
            const a = x.card.player.espn_player_id;
            const b = y.card.player.espn_player_id;
            return (
              <li key={`${a}-${b}`}>
                <Link href={pairFromShortlistHref(a, b, state)}>{playerName(x.card)} vs {playerName(y.card)}</Link>
              </li>
            );
          })}
        </ul>
      </Section>

      {(r.limitations.length > 0 || r.disclaimer) && (
        <Section title="Limits of this shortlist">
          {r.limitations.length > 0 ? <ul className={styles.limitList}>{r.limitations.map((l) => <li key={l}>{l}</li>)}</ul> : null}
          <p className={styles.disclaimerText}>{r.disclaimer}</p>
          <Method>
            <p>
              Each player is the same card as the two-player comparison: {best.source === "ours" ? "Fantasy Intel" : "ESPN"} projections for week {r.as_of_week} (where they exist) in this league&apos;s scoring, usage
              from completed weeks only (through week {r.completed_through_week}), and the latest injury designation. Slot eligibility is the lineup
              engine&apos;s, over the slots this league starts.
            </p>
            <p>
              Best projected is the highest stored projection among the players who can fill the slot. A player on bye, not eligible or without a
              projection is left out with the reason; when one has no projection the result says it is the best of the rest. Players within half a
              point of the top are tied. Nothing here is a probability, a ranking of its own or a consensus.
            </p>
          </Method>
        </Section>
      )}
      <Glossary terms={["Best projected", "Start pick", "Projected points", "ROS", "FLEX", "Snap share", "Target share", "xFP/g", "N/A", "—"]} providerProjections={profile.providerProjections} />
    </Shell>
  );
}

function SlotPicker({ r, state }: { r: ShortlistResponse; state: State }) {
  if (r.slots.length === 0) return null;
  const href = (slot: string | null) => shortlistHref({ ...state, slot });
  return (
    <nav className={cx("filters", styles.slotChips)} aria-label="Lineup slot">
      <span className="filter-label">Slot</span>
      <Link className="chip" href={href(null)} aria-current={r.slot === null ? "page" : undefined}>Any</Link>
      {r.slots.map((s) => (
        <Link key={s.slot} className="chip" href={href(s.slot)} aria-current={r.slot === s.slot ? "page" : undefined}
              title={`${s.slot}: ${s.positions.join(", ")}${s.count > 1 ? ` (${s.count} in the lineup)` : ""}`}>
          {s.slot}
        </Link>
      ))}
    </nav>
  );
}

function Cell({ c }: { c: ShortCell }) {
  if (c.kind === "na") return <span className={styles.na} title={c.why ?? undefined}>N/A<VisuallyHidden>{c.why ? `: ${c.why}` : ""}</VisuallyHidden></span>;
  if (c.kind === "missing") return <span className="missing" title={c.why ?? undefined}>—<VisuallyHidden>{c.why ? ` ${c.why}` : ""}</VisuallyHidden></span>;
  if (c.kind === "bye") return <span className={styles.bye} title={c.why ?? undefined}>Bye</span>;
  return <span className={styles.val} title={c.why ?? undefined}>{c.text}</span>;
}

type GridRow = { key: string; label: string; hint?: string; cell: (p: ShortlistPlayer) => ReactNode };

const text = (t: string | null, why: string): ShortCell => (t === null ? { text: DASH, kind: "missing", why } : { text: t, kind: "value", why: null });
const metric = (key: string, label: string, unit: RowUnit): GridRow => ({ key, label, cell: (p) => <Cell c={metricCell(p.card, key, unit)} /> });

function opponent(card: CompareCard): string | null {
  return nextOpponentText(card)?.replace(/^Wk \d+ /, "") ?? null;
}

/** Measures down, players across: four narrow columns fit a phone, and the table scrolls inside itself if not. */
function ShortlistGrid({ r, start, espnProjections }: { r: ShortlistResponse; start: number | null; espnProjections: boolean }) {
  const field = r.best_projected.source;
  const src = r.best_projected.source_label === "Unavailable" ? "Unavailable" : field === "ours" ? "Ours" : "ESPN";
  const week = r.as_of_week;
  const rows: GridRow[] = [];
  if (r.slot) {
    rows.push({
      key: "slot", label: `Can start at ${r.slot}`,
      cell: (p) => (p.slot_eligible
        ? <span className={styles.val}>Yes</span>
        : <span className={styles.no} title={`${p.card.player.position ?? "This position"} cannot start at ${r.slot}`}>No</span>),
    });
  }
  rows.push({
    key: "next", label: `Week ${week} proj`, hint: src,
    cell: (p) => (
      <>
        <Cell c={nextWeekCell(p.card, field)} />
        {isBestProjected(r, p.card.player.espn_player_id) ? <> <Badge value="Best" tone="info" title="Best projected" /></> : null}
      </>
    ),
  });
  if (field === "ours" && espnProjections) {
    rows.push({ key: "next_espn", label: `Week ${week} proj`, hint: "ESPN, reference", cell: (p) => <Cell c={nextWeekCell(p.card, "espn")} /> });
  }
  rows.push(
    { key: "opp", label: "Opponent", hint: `Week ${week}`, cell: (p) => <Cell c={text(opponent(p.card), "The league's projection window is unknown.")} /> },
    { key: "ros", label: "Rest of season", hint: src, cell: (p) => <Cell c={rosCell(p.card, field)} /> },
    {
      key: "games", label: "Games left",
      cell: (p) => {
        const left = p.card.projection?.games_remaining;
        return <Cell c={text(left === null || left === undefined ? null : String(left), "The NFL schedule is incomplete.")} />;
      },
    },
    { key: "bye", label: "Bye", cell: (p) => <Cell c={text(byeText(p.card)?.replace(/^Weeks? /, "") ?? null, "No bye week listed.")} /> },
    { key: "injury", label: "Injury", cell: (p) => <Cell c={injuryCell(p.card)} /> },
    {
      key: "roster", label: "Roster", hint: r.team?.name ?? undefined,
      cell: (p) => <span className={styles.val} title={rosterText(p, r.team?.name ?? null)}>{rosterShort(p.roster)}</span>,
    },
    { key: "role", label: "Role trend", cell: (p) => <Cell c={trendCell(p.card.role.role_trend, "Role trend")} /> },
    { key: "trend", label: "Usage trend", cell: (p) => <Cell c={trendCell(p.card.usage.trend.label, "Usage trend")} /> },
    metric("snap_share", "Snap share", "share"),
    metric("targets_pg", "Targets/g", "per_game"),
    metric("carries_pg", "Carries/g", "per_game"),
    metric("target_share", "Target share", "share"),
    metric("xfp_pg", "xFP/g", "pts_per_game"),
    metric("league_pts_pg", "League pts/g", "pts_per_game"),
  );
  return (
    <div className="table-wrap">
      <table className={styles.shortTable}>
        <caption className={styles.caption}>Shortlist of {r.players.length} players, measures down and players across</caption>
        <thead>
          <tr>
            <th scope="col">Measure</th>
            {r.players.map((p, i) => {
              const id = p.card.player.espn_player_id;
              const name = playerName(p.card);
              return (
                <th key={id} scope="col" className={styles.shortHead}>
                  <span className={styles.shortIndex} aria-hidden="true">{i + 1}</span>
                  <span className={styles.shortName} title={name}>{name}</span>
                  <span className={styles.shortMeta}>
                    {p.card.player.position ?? DASH}{start === id ? <> · <span className={styles.pickTag}>Start pick</span></> : null}
                  </span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <th scope="row" className={styles.measure}>
                <span className={styles.measureName}>{row.label}</span>
                {row.hint ? <span className={styles.unit}>{row.hint}</span> : null}
              </th>
              {r.players.map((p) => <td key={p.card.player.espn_player_id} className={styles.shortCell}>{row.cell(p)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PlayerRow({ p, i, r, state, teamName }: { p: ShortlistPlayer; i: number; r: ShortlistResponse; state: State; teamName: string | null }) {
  const id = p.card.player.espn_player_id;
  const name = playerName(p.card);
  const picked = state.start === id;
  return (
    <li className={styles.shortPlayer}>
      <span className={styles.shortIndex} aria-hidden="true">{i + 1}</span>
      <div className={styles.shortPlayerMain}>
        <PlayerIdentity id={id} name={p.card.player.full_name} position={p.card.player.position} team={p.card.player.pro_team}
                        meta={<span>{rosterText(p, teamName)}</span>} />
        <p className={styles.shortFacts}>
          {p.eligible_slots.length > 0 ? <>Can start at {p.eligible_slots.join(", ")}</> : <>No lineup slot of this league takes a {p.card.player.position ?? "player of unknown position"}</>}
          {" "}· Injury: {p.card.availability.has_designation ? p.card.availability.label : "no designation reported"}
          {isBestProjected(r, id) ? <> · <strong>Best projected</strong></> : null}
        </p>
      </div>
      <div className={styles.shortActions}>
        {picked
          ? <>
              <span className={styles.pickTag}>Your start pick</span>
              <Link className="chip reset" href={shortlistHref({ ...state, start: null })}>Clear pick<VisuallyHidden> for {name}</VisuallyHidden></Link>
            </>
          : <Link className="chip" href={shortlistHref({ ...state, start: id })}>Mark as my start<VisuallyHidden> for {name}</VisuallyHidden></Link>}
        <Link className="chip" href={shortlistHref({ ...state, ids: withoutPlayer(state.ids, id), start: picked ? null : state.start })}>
          Remove<VisuallyHidden> {name}</VisuallyHidden>
        </Link>
      </div>
    </li>
  );
}
