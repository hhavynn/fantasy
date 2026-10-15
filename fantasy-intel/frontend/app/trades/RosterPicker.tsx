"use client";

// One side of the trade builder: team selector, removable chips for the players picked, a searchable,
// position-filterable roster grouped by position, and the per-side limit message. Search and position
// filter are local view state; the selection itself lives in TradeBuilder (and reaches the URL on Evaluate).

import { useId, useState } from "react";
import { PlayerPhoto } from "@/components/PlayerPhoto";
import { Badge, Pos } from "@/components/ui";
import { cx } from "@/lib/cx";
import { filterRoster, groupByPosition, positionsIn, sideLimit } from "@/lib/trade-builder";
import type { OptionPlayer, OptionTeam } from "@/lib/trade-types";
import styles from "./builder.module.css";

function availabilityBadge(p: OptionPlayer) {
  const a = p.availability;
  if (a && ["nfl_ineligible", "no_eligible_week_remaining"].includes(a.status)) {
    const until = a.earliest_eligible_week ? `NFL IR · eligible wk ${a.earliest_eligible_week}` : "NFL IR · no week left";
    return <Badge value={until} tone="bad" title={a.warnings.join(" ")} />;
  }
  if (a && ["eligible_return_unconfirmed", "reserve_eligibility_unknown"].includes(a.status)) {
    return <Badge value={a.status === "reserve_eligibility_unknown" ? "IR · return unknown" : "IR · return unconfirmed"}
      tone="warn" title={a.warnings.join(" ")} />;
  }
  if (p.injury_status && !["ACTIVE", "NORMAL", "INJURY_RESERVE"].includes(p.injury_status)) return <Badge value={p.injury_status} />;
  return null;
}

const playerName = (p: OptionPlayer) => p.name ?? `#${p.espn_player_id}`;

export function RosterPicker({ role, team, teams, picked, maxPerSide, onTeam, onToggle }: {
  role: string;
  team: OptionTeam;
  teams: OptionTeam[];
  picked: number[];
  maxPerSide: number;
  onTeam: (teamId: number) => void;
  onToggle: (playerId: number) => void;
}) {
  const uid = useId();
  const [query, setQuery] = useState("");
  const [position, setPosition] = useState("");
  const limit = sideLimit(picked.length, maxPerSide);
  const chosen = picked.map((id) => team.roster.find((p) => p.espn_player_id === id)).filter((p): p is OptionPlayer => !!p);
  const shown = filterRoster(team.roster, query, position);
  const groups = groupByPosition(shown);
  const positions = positionsIn(team.roster);

  return (
    <section className={styles.side} aria-label={`${role}: ${team.name}`}>
      <div className={styles.sideHead}>
        <span className={styles.role}>{role}</span>
        <select className={styles.teamSelect} aria-label={`${role} team`} value={team.team_id} onChange={(e) => onTeam(Number(e.target.value))}>
          {teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
        </select>
      </div>

      <ul className={styles.chips} aria-label={`Players ${team.name} sends`}>
        {chosen.length === 0 && <li className={styles.chipsEmpty}>No players selected</li>}
        {chosen.map((p) => (
          <li key={p.espn_player_id} className={styles.chip}>
            <span className={styles.chipName}>{playerName(p)}</span>
            <Pos value={p.position} />
            <button type="button" className={styles.chipRemove} aria-label={`Remove ${playerName(p)}`} onClick={() => onToggle(p.espn_player_id)}>×</button>
          </li>
        ))}
      </ul>

      <div className={styles.find}>
        <input type="search" className={styles.search} placeholder="Search name or NFL team" aria-label={`Search ${team.name}'s roster`}
          value={query} onChange={(e) => setQuery(e.target.value)} />
        {positions.length > 1 && (
          <div className={styles.positions} role="group" aria-label="Filter by position">
            {["", ...positions].map((pos) => (
              <button key={pos || "all"} type="button" className={styles.pos} aria-pressed={position === pos} onClick={() => setPosition(pos)}>
                {pos === "" ? "All" : pos}
              </button>
            ))}
          </div>
        )}
      </div>

      <p className={cx(styles.limit, limit.atLimit && styles.limitFull)} role="status">
        {limit.atLimit
          ? `Limit reached: ${limit.count} of ${limit.max} players. Remove one to add another.`
          : `${limit.count} of ${limit.max} players (the analyzer's per-side limit)`}
      </p>

      <div className={styles.list}>
        {groups.length === 0 && <p className={styles.none}>No players match.</p>}
        {groups.map((g) => (
          <div key={g.position} className={styles.group}>
            <h3 className={styles.groupHead}><span>{g.position} · {g.players.length}</span><span>ESPN ROS</span></h3>
            <ul className={styles.rows}>
              {g.players.map((p) => {
                const on = picked.includes(p.espn_player_id);
                const blocked = !on && limit.atLimit;
                const id = `${uid}-${p.espn_player_id}`;
                return (
                  <li key={p.espn_player_id}>
                    <label htmlFor={id} className={cx(styles.row, on && styles.rowOn, blocked && styles.rowBlocked)}
                      title={blocked ? `Limit reached: remove a player to add ${playerName(p)}` : undefined}>
                      <input id={id} type="checkbox" className={styles.check} checked={on} disabled={blocked}
                        onChange={() => onToggle(p.espn_player_id)} />
                      <span className={styles.rowMain}>
                        <PlayerPhoto id={p.espn_player_id} name={playerName(p)} />
                        <span>{playerName(p)}</span>
                        {p.on_ir && <Badge value="fantasy IR slot" tone="bad" title="In an ESPN IR slot (inactive in the canonical lineup)" />}
                        {availabilityBadge(p)}
                      </span>
                      <span className={styles.nfl}>{p.pro_team ?? "FA"}</span>
                      <span className={styles.ros} title="ESPN rest-of-season projection (sum of weekly projections)">
                        {p.ros_projected_points === null
                          ? <span className="missing" title="Incomplete ESPN coverage">—</span>
                          : p.ros_projected_points.toFixed(1)}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
