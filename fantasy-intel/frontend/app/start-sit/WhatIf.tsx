import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { OriginTag } from "@/components/ProjectionNotice";
import { Badge, Delta, Notice, ProvenanceBadge, Section, Stat, StatStrip, VisuallyHidden } from "@/components/ui";
import { api } from "@/lib/api";
import { Missing, num } from "@/lib/format";
import { plural } from "@/lib/plural";
import { signalsOf } from "@/lib/signal-provenance";
import {
  carriedParams, moveText, optionLabel, restrictionLabel, shownDelta, shownTotal, whatIfHref, withWhatIf,
  type WhatIfPlayer, type WhatIfResponse, type WhatIfSlot,
} from "@/lib/what-if";
import { WhatIfPicker } from "./WhatIfPicker";
import styles from "./what-if.module.css";

type SP = Record<string, string | string[] | undefined>;

const PATH = "/start-sit";

/**
 * The what-if lineup builder: the engine's recommended lineup as a starting point, one "try a move" control per open
 * slot, and the totals recomputed by the backend against that recommendation. The scenario is the `whatif` URL
 * parameter; reset removes it. Read-only: nothing is submitted to the provider.
 */
export async function WhatIf({ apiPath, whatif, sp, provider, ours, invalidParam }: {
  /** `/api/start-sit/what-if?...` with the page's team, week and projection source. */
  apiPath: string;
  /** The URL's scenario, already bounded by readWhatIf (the backend validates it fully), or null. */
  whatif: string | null;
  sp: SP;
  provider: string;
  ours: boolean;
  /** The URL held a `whatif` value that is clearly not a scenario; it was not sent. */
  invalidParam: boolean;
}) {
  let res = await api<WhatIfResponse>(withWhatIf(apiPath, whatif));
  let rejectedLink = invalidParam;
  if (!res.ok && res.status === 422 && whatif) {
    // a malformed scenario (an edited or truncated link): show the recommendation and say so
    rejectedLink = true;
    res = await api<WhatIfResponse>(apiPath);
  }
  if (!res.ok) {
    return (
      <Section title="What-if lineup" id="what-if">
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </Section>
    );
  }
  const d = res.data;
  const players = new Map(d.players.map((p) => [p.player_id, p]));
  const slots = new Map(d.slots.map((s) => [s.slot_id, s]));
  const carried = carriedParams(sp);
  const applied = d.scenario.applied;
  const moves = d.scenario.moves;
  const rec = shownTotal(d.totals.recommended);
  const scen = shownTotal(d.totals.scenario);
  const delta = shownDelta(d.totals.delta, d.totals.known_delta);
  const missing = d.totals.scenario.missing_projection_ids.map((id) => players.get(id)?.name ?? `Player ${id}`);
  const last = moves.length > 0 ? moves[moves.length - 1] : null;
  const atLimit = moves.length >= d.scenario.move_limit;
  const resetHref = whatIfHref(PATH, sp, null);

  return (
    <Section
      id="what-if"
      title="What-if lineup"
      subtitle={`Try moves against the recommended lineup · nothing is sent to ${provider}`}
      actions={
        applied && moves.length > 0 ? (
          <span className={styles.actions}>
            {last && <Link href={whatIfHref(PATH, sp, last.whatif_without)} scroll={false} className="chip">Undo last move</Link>}
            <Link href={resetHref} scroll={false} className="chip reset">Reset to recommended</Link>
          </span>
        ) : null
      }
      footnote={d.limitations.join(" ")}
      flush
    >
      <div className={styles.head}>
        <StatStrip compact>
          <Stat label="Recommended (proj)" value={<>{num(rec.value, 1)}{rec.partial ? "*" : null}</>}
                note={rec.partial ? "*Known projections only" : "Best lineup under kickoff locks"} />
          <Stat label="What-if (proj)" value={<>{num(scen.value, 1)}{scen.partial ? "*" : null}</>}
                note={scen.partial ? "*Known projections only" : applied ? `${moves.length} ${plural(moves.length, "move")}` : "No moves yet"} />
          <Stat label="Versus recommended"
                tone={!delta.partial && delta.value < 0 ? "negative" : !delta.partial && delta.value > 0 ? "positive" : undefined}
                value={<><Delta value={delta.value} suffix=" pts" />{delta.partial ? "*" : null}</>}
                note={delta.partial ? "*Known projections only: some starters have none" : "Projected points"} />
          <Stat label="Changed slots" value={d.changed_slot_ids.length}
                note={d.changed_slot_ids.length > 0 ? d.changed_slot_ids.map((id) => slots.get(id)?.label ?? id).join(", ") : "Same as recommended"} />
        </StatStrip>
        <ProvenanceBadge signals={signalsOf(d.provenance)} />
      </div>

      <div className={styles.notices} aria-live="polite">
        {rejectedLink && (
          <Notice tone="caution" title="This what-if link could not be read">
            It is not a lineup scenario (it may have been edited or cut short), so the recommended lineup is shown.
          </Notice>
        )}
        {d.errors.length > 0 && (
          <Notice tone="caution" title="This what-if was not applied">
            <ul className={styles.problems}>
              {d.errors.map((e, i) => <li key={i}>{e.message}</li>)}
            </ul>
            The recommended lineup is shown instead. <Link href={resetHref} scroll={false}>Start again from the recommendation</Link>.
          </Notice>
        )}
        {missing.length > 0 && (
          <Notice tone="caution" title="Partial total">
            {missing.join(", ")} {plural(missing.length, "has", "have")} no projection: unknown, not zero. The what-if total
            counts known projections only.
          </Notice>
        )}
        {d.lineup_lock.engine_status && d.lineup_lock.engine_status !== "handled" && d.lineup_lock.engine_detail && (
          <p className={styles.lockNote}>{d.lineup_lock.engine_detail}</p>
        )}
        {atLimit && <p className={styles.lockNote}>Move limit reached ({d.scenario.move_limit}). Undo a move or reset to try more.</p>}
      </div>

      <div className="table-wrap">
        <table className={styles.table}>
          <caption className={styles.caption}>What-if lineup by slot, compared with the recommended lineup</caption>
          <thead>
            <tr>
              <th>Player</th>
              <th className="num">Proj</th>
              <th>Versus recommended</th>
              <th>Try a move</th>
            </tr>
          </thead>
          <tbody>
            {d.slots.map((s) => (
              <SlotRow key={s.slot_id} s={s} players={players} slots={slots} ours={ours} carried={carried} />
            ))}
          </tbody>
        </table>
      </div>

      {applied && moves.length > 0 && (
        <div className={styles.moves}>
          <h3 className={styles.movesTitle}>Your moves</h3>
          <ol className={styles.moveList}>
            {moves.map((m) => (
              <li key={m.index}>
                <span>{moveText(m, players, slots)}</span>{" "}
                {m.removable
                  ? <Link href={whatIfHref(PATH, sp, m.whatif_without)} scroll={false} className={styles.remove}>
                      Remove<VisuallyHidden> move {m.index + 1}</VisuallyHidden>
                    </Link>
                  : <span className={styles.kept} title={m.blocked_by ?? undefined}>Needed by a later move</span>}
              </li>
            ))}
          </ol>
        </div>
      )}
    </Section>
  );
}

function SlotRow({ s, players, slots, ours, carried }: {
  s: WhatIfSlot;
  players: Map<number, WhatIfPlayer>;
  slots: Map<string, WhatIfSlot>;
  ours: boolean;
  carried: [string, string][];
}) {
  const p = s.scenario_player_id !== null ? players.get(s.scenario_player_id) ?? null : null;
  const was = s.changed && s.recommended_player_id !== null ? players.get(s.recommended_player_id) ?? null : null;
  const tag = p ? restrictionLabel(p.restriction) : null;
  const options = s.options
    .filter((o) => o.whatif !== null)
    .map((o) => ({ value: o.whatif as string, label: optionLabel(o, players, slots) }));
  return (
    <tr className={s.changed ? styles.changed : undefined}>
      <td>
        {p ? (
          <PlayerIdentity id={p.player_id} name={p.name} position={p.position} team={p.pro_team}
                          lead={<span title="Lineup slot">{s.label}</span>}
                          meta={tag ? <Badge value={tag} tone="" title={p.restriction_reason ?? undefined} /> : null} />
        ) : (
          <span className={styles.lineCell}><span className={styles.slotName}>{s.label}</span><span className={styles.empty}>Empty</span></span>
        )}
      </td>
      <td className="num">
        {p ? <><OriginTag origin={p.projection_origin} ours={ours} /> <b>{num(p.projected_points, 1, "No projection: unknown, not zero")}</b></> : <Missing why="No player in this slot" />}
      </td>
      <td className={styles.change}>
        {s.changed
          ? <><Badge value="Changed" tone="info" /> {was ? <>was {was.name}</> : "was empty"}</>
          : <span className="faint">Recommended</span>}
      </td>
      <td className={styles.tryCell}>
        {s.locked ? (
          <span className={styles.locked}><Badge value="Locked" tone="" /> {s.lock_reason}</span>
        ) : options.length > 0 ? (
          <WhatIfPicker slotLabel={s.label} options={options} carried={carried} />
        ) : (
          <span className="faint">{s.options.length > 0 ? "Move limit reached" : "No other eligible player"}</span>
        )}
        {!s.locked && (
          <p className={styles.eligible}>Takes {s.eligible_positions.join(", ")}</p>
        )}
        {s.blocked.length > 0 && (
          <details className={styles.blocked}>
            <summary>Not available here ({s.blocked.length})</summary>
            <ul>
              {s.blocked.map((b) => <li key={b.player_id}>{b.message}</li>)}
            </ul>
          </details>
        )}
      </td>
    </tr>
  );
}
