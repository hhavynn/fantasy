import { when } from "@/lib/format";
import { getLeagueProfile } from "@/lib/league-profile.server";
import {
  COMPONENT_SHORT_UNIT, componentValueText, movementText, ordinal, pctText, recordText, weightText,
  type PowerRankings, type PrTeam,
} from "@/lib/power-rankings";
import { ScoreBar } from "./parts";
import styles from "./power.module.css";

const SLOT_TEXT: Record<string, string> = { FLEX: "Flex", "D/ST": "D/ST" };
const slotText = (s: string) => SLOT_TEXT[s] ?? s;

/**
 * One team's power ranking as a self-contained card: rank and movement, the power score, the four components with
 * their league scores and ranks, where the lineup is strongest and thinnest, and who would be hardest to replace.
 * Sized and framed for a screenshot; every number is the backend's.
 */
export async function TeamCard({ d, team, heading = "h2" }: { d: PowerRankings; team: PrTeam; heading?: "h2" | "h3" }) {
  const { providerProjections } = await getLeagueProfile();
  const H = heading;
  const ranked = d.teams.filter((t) => t.rank !== null).length;
  const move = movementText(team);
  const titleId = `card-${team.team_id}`;
  const keyPlayers = team.depth?.key_players ?? [];
  return (
    <article className={styles.card} aria-labelledby={titleId}>
      <p className={styles.cardKicker}>
        <span>{d.league.name}</span>
        <span>Power rankings · Week {d.week}</span>
      </p>

      <div className={styles.cardHero}>
        <p className={styles.cardRank} aria-label={team.rank ? `Ranked ${ordinal(team.rank)} of ${ranked}` : "Unranked"}>
          <span className={styles.cardHash} aria-hidden="true">#</span>{team.rank ?? "—"}
        </p>
        <div className={styles.cardWho}>
          <H id={titleId} className={styles.cardName}>{team.name}</H>
          <p className={styles.cardSub}>
            {recordText(team.record)} record · all-play {recordText(team.all_play)} ({pctText(team.all_play.pct)})
          </p>
          <p className={styles.cardMove} data-dir={team.movement === null ? undefined : team.movement > 0 ? "up" : team.movement < 0 ? "down" : "flat"}>
            {move ?? (d.previous.available ? "Not ranked last week" : "No movement this week")}
          </p>
        </div>
        <div className={styles.cardScore}>
          <span className={styles.cardLabel}>Power score</span>
          <strong>{team.power_score === null ? "—" : team.power_score.toFixed(1)}</strong>
          <ScoreBar score={team.power_score} label="Power score" strong />
        </div>
      </div>

      {team.rank === null ? (
        <p className={styles.cardNote}>Not ranked this week: {team.unranked_reason}.</p>
      ) : (
        <dl className={styles.cardComponents}>
          {d.components.map((c) => {
            const v = team.components[c.key];
            return (
              <div key={c.key} className={styles.cardComponent}>
                <dt>
                  {c.label}
                  <span className={styles.weight}>{weightText(d.weights[c.key])}</span>
                </dt>
                <dd className={styles.cardValue}>
                  <strong>{componentValueText(c.key, v.value)}</strong> <span>{COMPONENT_SHORT_UNIT[c.key]}</span>
                </dd>
                <dd className={styles.cardBar}>
                  <ScoreBar score={v.score} label={`${c.label} score`} />
                  <span>{v.rank ? `${ordinal(v.rank)} of ${ranked}` : "—"}</span>
                </dd>
              </div>
            );
          })}
        </dl>
      )}

      {(team.positions || keyPlayers.length > 0 || team.replacement) && (
        <div className={styles.cardFacts}>
          {team.positions && (
            <p>
              <span className={styles.cardLabel}>Strongest</span>
              {team.positions.strongest.map((s) => `${slotText(s.slot)} (${ordinal(s.rank)})`).join(", ")}
              <span className={styles.factSep} aria-hidden="true" />
              <span className={styles.cardLabel}>Thinnest</span>
              {team.positions.weakest.map((s) => `${slotText(s.slot)} (${ordinal(s.rank)})`).join(", ")}
            </p>
          )}
          {keyPlayers.length > 0 && (
            <p>
              <span className={styles.cardLabel}>Hardest to replace</span>
              {keyPlayers.map((p) => `${p.name} (${p.position}, −${(p.marginal ?? 0).toFixed(0)} pts)`).join(", ")}
            </p>
          )}
          {team.replacement && (
            <p>
              <span className={styles.cardLabel}>Backups above waivers</span>
              {team.replacement.covered} of {team.replacement.of} positions
              {team.replacement.positions.length > 0 && (
                <> ({team.replacement.positions.map((p) => `${p.position} ${p.has_backup ? `+${(p.edge ?? 0).toFixed(1)}` : "none"}`).join(", ")})</>
              )}
            </p>
          )}
        </div>
      )}

      <p className={styles.cardFoot}>
        Fantasy Intel · {providerProjections ? "ESPN projections" : "own projections (Ours)"} and completed weeks, scaled within this league. A ranking, not a win probability.
        {d.freshness.league_synced_at && <> Synced {when(d.freshness.league_synced_at)}.</>}
      </p>
    </article>
  );
}
