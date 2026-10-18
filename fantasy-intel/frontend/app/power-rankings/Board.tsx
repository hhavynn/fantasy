import { cx } from "@/lib/cx";
import { Missing } from "@/lib/format";
import { cardHref, COMPONENT_KEYS, pctText, recordText, weightText, type PowerRankings, type PrTeam } from "@/lib/power-rankings";
import { ComponentCell, Movement, ScoreBar, TeamName } from "./parts";
import styles from "./power.module.css";

/** The league table (desktop) and a compact ranked list (phones); CSS shows one of them. */
export function Board({ d, focus }: { d: PowerRankings; focus: number | null }) {
  const ranked = d.teams.filter((t) => t.rank !== null).length;
  const label = Object.fromEntries(d.components.map((c) => [c.key, c.label])) as Record<string, string>;
  const noPrev = d.previous.available ? "No rank last week" : (d.previous.reason ?? "Last week's ranks are unavailable");
  return (
    <>
      <div className={cx("table-wrap", styles.tableView)}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col" className={styles.rankCol}>Rank</th>
              <th scope="col" className={styles.teamCol}>Team</th>
              <th scope="col" className="num">Power</th>
              {d.components.map((c) => (
                <th key={c.key} scope="col" className="num" title={c.description}>
                  {c.label}<span className={styles.weight}>{weightText(d.weights[c.key])}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.teams.map((t) => (
              <tr key={t.team_id} className={cx(t.team_id === focus && "mine")}>
                <td className={styles.rankCol}>
                  <span className={styles.rankCell}>
                    <strong>{t.rank ?? <Missing why={t.unranked_reason ?? "Unranked"} />}</strong>
                    <Movement team={t} why={noPrev} />
                  </span>
                </td>
                <td className={styles.teamCol}>
                  <TeamName team={t} href={cardHref(t.team_id)} />
                  <span className={styles.sub}>{recordText(t.record)}{t.is_mine ? " · your team" : ""}</span>
                </td>
                <td className="num">
                  {t.power_score === null ? <Missing why={t.unranked_reason ?? "Unranked"} /> : (
                    <span className={styles.power}>
                      <strong>{t.power_score.toFixed(1)}</strong>
                      <ScoreBar score={t.power_score} label="Power score" strong />
                    </span>
                  )}
                </td>
                {COMPONENT_KEYS.map((k) => (
                  <td key={k} className="num">
                    <ComponentCell team={t} k={k} label={label[k]} total={ranked} />
                    {k === "all_play" && t.components.all_play.value !== null && (
                      <span className={styles.sub}>{recordText(t.all_play)}</span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ol className={styles.listView} aria-label="Power rankings">
        {d.teams.map((t) => <PhoneRow key={t.team_id} t={t} d={d} focus={focus} noPrev={noPrev} label={label} />)}
      </ol>
    </>
  );
}

function PhoneRow({ t, d, focus, noPrev, label }: {
  t: PrTeam; d: PowerRankings; focus: number | null; noPrev: string; label: Record<string, string>;
}) {
  return (
    <li className={cx(styles.row, t.team_id === focus && styles.rowFocus)}>
      <div className={styles.rowHead}>
        <span className={styles.rowRank}>{t.rank ?? "—"}</span>
        <div className={styles.rowTeam}>
          <TeamName team={t} href={cardHref(t.team_id)} />
          <span className={styles.sub}>
            {recordText(t.record)} · all-play {pctText(t.all_play.pct)}{t.is_mine ? " · your team" : ""}
          </span>
        </div>
        <div className={styles.rowScore}>
          <strong>{t.power_score === null ? "—" : t.power_score.toFixed(1)}</strong>
          <Movement team={t} why={noPrev} />
        </div>
      </div>
      {t.rank === null ? (
        <p className={styles.sub}>Unranked: {t.unranked_reason}</p>
      ) : (
        <dl className={styles.rowComponents}>
          {d.components.map((c) => (
            <div key={c.key}>
              <dt>{c.label}</dt>
              <dd><ComponentCell team={t} k={c.key} label={label[c.key]} total={d.teams.filter((x) => x.rank !== null).length} /></dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}
