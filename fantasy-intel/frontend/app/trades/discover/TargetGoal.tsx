import { BackendProblem } from "@/components/BackendProblem";
import { TeamLink } from "@/components/TeamLink";
import { Notice } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { horizonOptions } from "@/lib/trade-discovery";
import type { DiscoveryContext, DiscoveryTarget, DiscoveryTargets, TargetFit } from "@/lib/trade-types";
import styles from "./discover.module.css";

// Targeted discovery (issue #182): the goal form (a player, a position, a roster need, a horizon) and the per-row
// trade-off list. A target only narrows which trades are searched and shown; it never changes how a trade is valued,
// and nothing here predicts what the other manager will do.

const PHASE_LABEL: Record<string, string> = { ros: "Rest of season", regular: "Regular season", playoff: "Fantasy playoffs" };
const NEED_LABEL: Record<string, string> = { weakest: "Upgrade my weakest position", bye: "Cover my bye week" };

/** The goal form at the top of the targeted lens. Every field is optional; the form is a plain GET of this page (`action`:
 *  the page under the current league's /l/<lg>/ prefix on a league page). */
export async function TargetGoal({ c, targets, action }: {
  c: DiscoveryContext; targets: Promise<ApiResult<DiscoveryTargets>>; action: string;
}) {
  const res = await targets;
  if (!res.ok) return <BackendProblem error={res.error} kind={res.kind} status={res.status} />;
  const t = res.data;
  const q = c.query;
  const byTeam = new Map<number, { name: string; players: DiscoveryTargets["players"] }>();
  for (const p of t.players) {
    const g = byTeam.get(p.team_id) ?? { name: p.team_name ?? `Team ${p.team_id}`, players: [] };
    g.players.push(p);
    byTeam.set(p.team_id, g);
  }
  const needText = (k: "weakest" | "bye") => {
    const n = t.need_resolution[k];
    return n.resolved ? `${NEED_LABEL[k]} (${k === "weakest" ? n.position : `week ${n.week}`})` : `${NEED_LABEL[k]} (none found)`;
  };
  return (
    <form action={action} className={`panel ${styles.goal}`} aria-label="What you are looking for">
      <input type="hidden" name="mode" value="targeted" />
      <input type="hidden" name="team" value={q.team_id} />
      <p className={styles.goalTitle}>What are you looking for?</p>
      <div className={styles.row}>
        <label className={styles.field}>Player to get
          <select name="target" defaultValue={q.target_player ?? ""}>
            <option value="">Any player</option>
            {[...byTeam.entries()].map(([id, g]) => (
              <optgroup key={id} label={g.name}>
                {g.players.map((p) => (
                  <option key={p.espn_player_id} value={p.espn_player_id}>
                    {p.name ?? `#${p.espn_player_id}`} · {p.position ?? "—"}{p.on_ir ? " · fantasy IR" : ""}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label className={styles.field}>Position to get
          <select name="pos" defaultValue={q.target_position ?? ""}>
            <option value="">Any position</option>
            {t.positions.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label className={styles.field}>Roster need
          <select name="need" defaultValue={q.need ?? ""}>
            <option value="">None</option>
            <option value="weakest" title={t.need_resolution.weakest.reason}>{needText("weakest")}</option>
            <option value="bye" title={t.need_resolution.bye.reason}>{needText("bye")}</option>
          </select>
        </label>
        <label className={styles.field}>Horizon
          <select name="weeks" defaultValue={q.weeks ?? ""}>
            {horizonOptions(c.evaluation.start_week, c.evaluation.final_week).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>
        <label className={styles.field}>Season phase
          <select name="phase" defaultValue={q.phase}>
            {c.phases.map((p) => <option key={p} value={p}>{PHASE_LABEL[p] ?? p}</option>)}
          </select>
        </label>
        <div className={styles.actions}>
          <button type="submit">Find trades</button>
          <TeamLink href="/trades/discover?mode=targeted">Clear</TeamLink>
        </div>
      </div>
      <p className={styles.footnote}>
        A roster need picks its own position or week: it replaces the position or horizon chosen above. Your weakest position: {t.need_resolution.weakest.reason}{" "}
        Bye coverage: {t.need_resolution.bye.reason} A target only narrows the search; it never changes how a trade is valued.
      </p>
    </form>
  );
}

/** One sentence naming what a targeted (or target-filtered) view brings in, or null when nothing is targeted. */
export function targetSentence(target: DiscoveryTarget | undefined, horizonLabel: string | null): string | null {
  if (!target?.active) return null;
  const what = target.player
    ? `${target.player.name ?? `#${target.player.espn_player_id}`} (${target.player.position ?? "—"}, ${target.player.team_name ?? `Team ${target.player.team_id}`})`
    : target.position ? `at least one ${target.position}` : null;
  const parts = [what ? `Trades in which you receive ${what}` : "Trades of any position"];
  if (target.weeks?.length && horizonLabel) parts.push(`judged on your gain over ${horizonLabel}`);
  return `${parts.join(", ")}.`;
}

/** The target and how any roster need resolved, above the results. An unresolved need says why, in words. */
export function TargetSummary({ target, horizonLabel }: { target: DiscoveryTarget | undefined; horizonLabel: string | null }) {
  const sentence = targetSentence(target, horizonLabel);
  const need = target?.need;
  if (!sentence && !need) return null;
  return (
    <>
      {sentence && <Notice tone="info">{sentence}{need?.resolved ? ` ${need.reason}` : ""}</Notice>}
      {need && !need.resolved && <Notice tone="caution" title="Roster need not found">{need.reason}</Notice>}
    </>
  );
}

/** A targeted row's trade-offs: horizon against the rest of the season, the target's use, depth, roster fit, partner. */
export function TargetFitNotes({ fit }: { fit?: TargetFit }) {
  if (!fit?.tradeoffs.length) return null;
  return (
    <ul className={styles.fitList} aria-label="Trade-offs">
      {fit.tradeoffs.map((t, i) => <li key={i} data-kind={t.kind}>{t.text}</li>)}
    </ul>
  );
}
