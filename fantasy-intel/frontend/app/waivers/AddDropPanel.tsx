import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { Delta, Notice, ProvenanceBadge, Skeleton, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import { Missing, num, when } from "@/lib/format";
import { plural } from "@/lib/plural";
import { signalsOf } from "@/lib/signal-provenance";
import {
  addDropHref, carriedParams, headline, optionLabel, shownChange, windowLabel, withAddDrop,
  type AddDropResponse, type AddDropSelection, type AddDropWeek,
} from "@/lib/add-drop";
import { AddDropForm } from "./AddDropForm";
import styles from "./add-drop.module.css";

type SP = Record<string, string | string[] | undefined>;

const TITLE = "Add/drop simulator";

/** A change with its known-only label: the full delta, or the sum of known projections marked as such. */
function Change({ full, known, what }: { full: number | null; known: number | null | undefined; what: string }) {
  const c = shownChange(full, known);
  if (c.value === null) return <Missing why={`No projected ${what}`} />;
  return (
    <span title={c.partial ? "A projection is missing: this sums known projections only, never counting the missing one as 0" : undefined}>
      <Delta value={c.value} suffix=" pts" />
      {c.partial && <span className={styles.partial}> known-only</span>}
    </span>
  );
}

function names(rows: { espn_player_id: number; name: string | null }[]): string {
  return rows.map((r) => r.name ?? `Player ${r.espn_player_id}`).join(", ");
}

function byeText(w: AddDropWeek, who: Map<number, string>): string | null {
  if (w.on_bye_after === null) return null;
  return w.on_bye_after.length === 0 ? null : w.on_bye_after.map((id) => who.get(id) ?? `Player ${id}`).join(", ");
}

/**
 * The add/drop simulator beside Top Claims (issue #178): pick one available player to add and one rostered player to
 * drop (or only one side), and the backend values the move with the trade analyzer's exact weekly lineup simulation.
 * The scenario is the `add` and `drop` URL parameters. Analysis only: nothing is sent to the provider.
 */
export async function AddDropPanel({ apiPath, sel, sp, provider }: {
  /** `/api/waivers/add-drop` with the page's team. */
  apiPath: string;
  sel: AddDropSelection;
  sp: SP;
  provider: string;
}) {
  let res = await api<AddDropResponse>(withAddDrop(apiPath, sel));
  let rejected = sel.kind === "invalid";
  if (!res.ok && res.status === 422 && sel.kind === "pair") {
    // an edited or stale link (a player not in this league's data): show the empty simulator and say so
    rejected = true;
    res = await api<AddDropResponse>(apiPath);
  }
  if (!res.ok) {
    return (
      <section className={styles.panel} id="add-drop" aria-label={TITLE}>
        <h2 className={styles.title}>{TITLE}</h2>
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </section>
    );
  }
  const d = res.data;
  const carried = carriedParams(sp);
  const ev = d.evaluation;
  const span = windowLabel(ev.weeks);
  const first = d.impact?.first_week ?? null;
  const ros = d.impact?.rest_of_season ?? null;
  const who = new Map<number, string>([
    ...d.options.drop.map((o) => [o.espn_player_id, o.name ?? `Player ${o.espn_player_id}`] as [number, string]),
    ...(d.players.add ? [[d.players.add.espn_player_id, d.players.add.name ?? `Player ${d.players.add.espn_player_id}`] as [number, string]] : []),
  ]);
  const clearHref = addDropHref("", sp, { add: null, drop: null });
  const suggested = d.roster.suggested_drop;
  const changedDepth = d.depth.filter((p) => p.active_change !== null && p.active_change !== 0);

  return (
    <section className={styles.panel} id="add-drop" aria-label={TITLE}>
      <div className={styles.head}>
        <div>
          <p className={styles.eyebrow}>Analysis only · nothing is sent to {provider}</p>
          <h2 className={styles.title}>{TITLE}</h2>
        </div>
        {d.provenance && <ProvenanceBadge signals={signalsOf(d.provenance)} />}
      </div>

      {rejected && (
        <Notice tone="caution" title="This link's move was not simulated">
          It names a player who is not in this league&apos;s data, or the same player twice. Choose the players again.
        </Notice>
      )}

      {/* Keyed on the scenario: the selects are uncontrolled (defaultValue), so a client navigation to another move
          (a Simulate link, Clear, Back/Forward) must remount them or they would keep showing the previous players. */}
      <AddDropForm
        key={`${d.request.add ?? ""}|${d.request.drop ?? ""}`}
        addOptions={d.options.add.map((o) => ({ value: String(o.espn_player_id), label: optionLabel(o) }))}
        dropOptions={d.options.drop.map((o) => ({ value: String(o.espn_player_id), label: optionLabel(o) }))}
        add={d.request.add !== null && d.options.add.some((o) => o.espn_player_id === d.request.add) ? String(d.request.add) : ""}
        drop={d.request.drop !== null && d.options.drop.some((o) => o.espn_player_id === d.request.drop) ? String(d.request.drop) : ""}
        carried={carried}
      />
      <p className={styles.sub}>
        Add lists the {d.options.add.length} best-projected of {d.options.add_total} available {plural(d.options.add_total, "player")} (free agents and waivers; rostered players never appear).
      </p>

      <div className={styles.result} aria-live="polite">
        <p className={styles.headline}>{headline(d)}</p>
        {d.status !== "options" && <Link href={clearHref} scroll={false} className="chip reset">Clear move</Link>}

        {d.problems.map((p) => (
          <Notice key={`${p.code}-${p.player_id}`} tone="caution" title={p.code === "DROP_REQUIRED" ? "A drop is required" : "Not possible as asked"}>
            {p.message}
            {p.code === "DROP_REQUIRED" && suggested?.available && (
              <> {" "}
                <Link href={addDropHref("", sp, { add: d.request.add, drop: suggested.espn_player_id })} scroll={false}>
                  Try dropping {suggested.name ?? `player ${suggested.espn_player_id}`}
                </Link>
                {" "}(costs {suggested.lineup_cost !== null ? `${suggested.lineup_cost.toFixed(1)}` : `${suggested.known_lineup_cost.toFixed(1)} known-only`} projected starting points; the trade analyzer&apos;s forced-drop rule).
              </>
            )}
          </Notice>
        ))}

        {d.impact && first && ros && (
          <StatStrip compact className={styles.figures}>
            <Stat label={`Week ${first.week}`} value={<Change full={first.delta} known={first.known_delta} what="change" />}
              note={first.week === ev.earliest_actionable_week ? "first week a move can change" : undefined} />
            <Stat label={span ? `Rest of season, ${span.toLowerCase()}` : "Rest of season"} value={<Change full={ros.ros_delta} known={ros.known_ros_delta} what="change" />}
              note={`${ros.weeks_improved} better · ${ros.weeks_worsened} worse${ros.weeks_incomplete.length ? ` · ${ros.weeks_incomplete.length} incomplete` : ""}`} />
            <Stat label="Active roster" value={d.roster.active_after !== null ? `${d.roster.active_after} of ${d.roster.active_capacity}` : <Missing />}
              note={d.roster.open_spots_after !== null ? `${d.roster.open_spots_after} open ${plural(d.roster.open_spots_after, "spot")}` : undefined} />
          </StatStrip>
        )}

        {d.notes.length > 0 && (
          <ul className={styles.notes}>
            {d.notes.map((n) => <li key={`${n.code}-${n.player_id}`} data-tone={n.tone}>{n.message}</li>)}
          </ul>
        )}

        {d.acquisition && <p className={styles.sub}><b>Acquisition.</b> {d.acquisition.summary}</p>}

        {d.impact && first && (
          <p className={styles.sub}>
            Week {first.week} lineup: {first.entered.length ? <>{names(first.entered)} {first.entered.length === 1 ? "starts" : "start"}</> : "no new starter"}
            {first.exited.length ? <>; {names(first.exited)} {first.exited.length === 1 ? "leaves" : "leave"} the lineup</> : null}.
          </p>
        )}

        {d.impact && (
          <details className={styles.details}>
            <summary>Week by week, depth and byes</summary>
            <div className="table-wrap">
              <table className={styles.table}>
                <caption className="sub">Projected starting points, optimal lineup before and after the move</caption>
                <thead>
                  <tr><th scope="col">Week</th><th scope="col">Before</th><th scope="col">After</th><th scope="col">Change</th><th scope="col">On bye</th></tr>
                </thead>
                <tbody>
                  {d.impact.weeks.map((w) => (
                    <tr key={w.week}>
                      <th scope="row">{w.week}{w.phase === "playoff" ? " (playoffs)" : ""}</th>
                      <td>{num(w.before ?? null, 1, `Known-only: ${w.known_before.toFixed(1)} (a projection is missing)`)}</td>
                      <td>{num(w.after ?? null, 1, `Known-only: ${w.known_after.toFixed(1)} (a projection is missing)`)}</td>
                      <td><Change full={w.delta} known={w.known_delta} what="change" />{w.open_slots_after > w.open_slots_before && <span className={styles.partial}> {w.open_slots_after - w.open_slots_before} empty {plural(w.open_slots_after - w.open_slots_before, "slot")}</span>}</td>
                      <td>{w.on_bye_after === null ? <Missing why="The NFL schedule does not cover this week" /> : byeText(w, who) ?? "None"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h3 className={styles.h3}>Depth</h3>
            <ul className={styles.depth}>
              {d.depth.map((p) => (
                <li key={p.position}>
                  <b>{p.position}</b> {p.before.active}
                  {p.after && p.after.active !== p.before.active && <> → {p.after.active}</>} active
                  {p.before.reserved > 0 && <span className="sub"> · {p.before.reserved} IR/taxi</span>}
                  <span className="sub"> · starts {p.starting_slots}{p.flex_slots.length ? ` + ${p.flex_slots.map((f) => `${f.count} ${f.slot}`).join(", ")}` : ""}</span>
                </li>
              ))}
            </ul>
            {changedDepth.length === 0 && <p className="sub">Positional depth is unchanged.</p>}
            {d.players.add && <ByeLine label="Added player's byes" player={d.players.add} />}
            {d.players.drop && <ByeLine label="Dropped player's byes" player={d.players.drop} />}
          </details>
        )}

        <p className={styles.window}>
          {span ? <>{span}</> : "No remaining weeks"} · {d.projection_source} projections · computed {when(ev.evaluated_at)}.
          {ev.start_week_reason.reason ? <> Window: {String(ev.start_week_reason.reason)}.</> : null}
          {ev.uncertain && ev.warning ? <> {ev.warning}</> : null}
        </p>
      </div>
    </section>
  );
}

function ByeLine({ label, player }: { label: string; player: NonNullable<AddDropResponse["players"]["add"]> }) {
  const byes = player.bye_weeks;
  return (
    <p className={styles.sub}>
      {label} ({player.name ?? `Player ${player.espn_player_id}`}): {byes === undefined || byes === null
        ? <Missing why="The NFL schedule does not cover the remaining weeks" />
        : byes.length === 0 ? "none remaining" : `week ${byes.join(", ")}`}
    </p>
  );
}

/** The same footprint while the simulation loads. */
export function AddDropSkeleton() {
  return (
    <section className={styles.panel} aria-label={TITLE} aria-busy="true">
      <h2 className={styles.title}>{TITLE}</h2>
      <Skeleton h={44} />
      <Skeleton h={44} />
      <Skeleton w="60%" h={14} />
    </section>
  );
}
