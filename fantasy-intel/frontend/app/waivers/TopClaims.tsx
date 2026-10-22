import { SavePlayer } from "./Shortlist";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Badge, Score, Verdict } from "@/components/ui";
import type { TopClaims as Top } from "@/lib/waivers";
import { Competition, EvidenceBadge, GiveUp, OwnedDelta, RoleTrend, actionLabel } from "./WaiverParts";
import { hasInjuryDesignation } from "../players/helpers";
import styles from "./waivers.module.css";
import type { WaiverRow } from "./types";

/**
 * The few moves worth looking at first. Compact cards are right here: three comparable options,
 * each with the same facts, side by side. Without an actionable move the band says so and shows
 * the best-ranked options that are not an outright pass, labelled as such.
 */
/** `provider` names the league's data source; `ownership` false (Sleeper) leaves out the % owned line. */
export function TopClaims({ top, position, provider = "ESPN", ownership = true }: {
  top: Top<WaiverRow>; position: string; provider?: string; ownership?: boolean;
}) {
  const scope = position === "ALL" ? "" : ` at ${position}`;
  const heading = top.mode === "actionable" ? `Top claims${scope}` : top.mode === "watch" ? `Nothing to claim right now${scope}` : `No candidates${scope}`;
  const count = top.mode === "none" ? 0 : top.rows.length;
  return (
    <section className={styles.band} aria-label="Top claims">
      <Verdict
        tone={top.mode === "actionable" ? "positive" : "info"}
        eyebrow={top.mode === "actionable" ? `${count} ${count === 1 ? "move" : "moves"}, best first` : "No claim recommended"}
        headline={heading}
      >
        {top.mode === "actionable" && "Moves the analysis calls actionable for this roster, each with the player to drop. Evidence is under each player in the list below."}
        {top.mode === "watch" && "No add clears the bar for a claim. These are the highest-ranked options that are not an outright pass; none is a recommendation to add."}
        {top.mode === "none" && `Every evaluated player${scope} is a pass for this roster. Refresh ${provider} and NFL data if the snapshot looks stale.`}
      </Verdict>
      {top.mode !== "none" && (
        <ol className={styles.claims}>
          {top.rows.map((w, i) => <ClaimCard key={w.espn_player_id} row={w} rank={i + 1} informational={top.mode !== "actionable"} ownership={ownership} />)}
        </ol>
      )}
    </section>
  );
}

function ClaimCard({ row: w, rank, informational, ownership }: { row: WaiverRow; rank: number; informational: boolean; ownership: boolean }) {
  const d = w.components.decision;
  // In the "nothing to claim" band no add is recommended, so "no drop found" is not a failed action:
  // the Give up row appears only when the analysis did name a player to drop.
  const showGiveUp = !informational || Boolean(d?.roster_move.drop);
  const summary = d ? d.streaming?.summary ?? d.utility.summary : null;
  const names = w.competitors.map((c) => c.name);
  return (
    <li className={styles.claim} data-actionable={informational ? undefined : "true"}>
      <div className={styles.claimHead}>
        <span className={styles.claimRank} aria-label={`Rank ${rank}`}>{rank}</span>
        <h3 className={styles.claimName}>
          <PlayerIdentity
            id={w.espn_player_id} name={w.full_name} position={w.position} team={w.pro_team} size="lg"
            meta={<>
              {ownership && <span><OwnedDelta row={w} /> owned</span>}
              {hasInjuryDesignation(w.injury_status) && <Badge value={w.injury_status} />}
            </>}
          />
        </h3>
      </div>
      <SavePlayer id={w.espn_player_id} name={w.full_name} />
      <div className={styles.claimAction}>
        <Badge value={w.recommendation} />
        <strong>{actionLabel(w) ?? "Refresh analytics"}</strong>
        <EvidenceBadge row={w} />
      </div>
      {summary && <p className={styles.claimSummary}>{summary}</p>}
      <dl className={styles.claimFacts}>
        <dt>Waiver score</dt><dd><Score value={w.waiver_score} /></dd>
        <dt>Role trend</dt><dd><RoleTrend trend={w.role_trend} /></dd>
        <dt>Competition</dt>
        <dd>
          <Competition row={w} />
          {names.length > 0 && (
            <span className={styles.competitors}>
              {" "}{names.slice(0, 2).map((n, i) => (
                <span key={n}>{i > 0 && ", "}{n}</span>
              ))}{names.length > 2 && ` +${names.length - 2}`}
            </span>
          )}
        </dd>
        {showGiveUp && <><dt>Give up</dt><dd><GiveUp row={w} /></dd></>}
      </dl>
    </li>
  );
}

