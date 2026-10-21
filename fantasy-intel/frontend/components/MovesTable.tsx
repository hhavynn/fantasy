import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Pos } from "@/components/ui";
import { date } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import type { Move } from "@/lib/types";

export async function MovesTable({ moves, showTeam = true }: { moves: Move[]; showTeam?: boolean }) {
  if (moves.length === 0) return <p className="faint">No transactions recorded.</p>;
  const prov = providerName((await getLeagueProfile()).provider);
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Date</th><th>Wk</th>{showTeam && <th>Team</th>}<th>Move</th><th>Player</th><th className="num">Bid</th></tr></thead>
        <tbody>
          {moves.map((m, i) => (
            <tr key={`${m.transaction_id}-${m.espn_player_id}-${i}`}>
              <td>{date(m.date, prov)}</td>
              <td>{m.scoring_period ?? ""}</td>
              {showTeam && <td>{m.item_type === "TRADE" ? `${m.from_team_name ?? "?"} → ${m.to_team_name ?? "?"}` : m.team_name}</td>}
              <td><Badge value={m.item_type === "ADD" ? (m.type === "WAIVER" ? "WAIVER ADD" : "FA ADD") : m.item_type}
                tone={m.item_type === "ADD" ? "good" : m.item_type === "DROP" ? "bad" : "info"} /></td>
              <td><PlayerLink id={m.espn_player_id} name={m.full_name} /> {m.position && <Pos value={m.position} />}</td>
              <td className="num">{m.bid_amount ? `$${m.bid_amount}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
