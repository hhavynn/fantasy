"use client";

import { TeamLink } from "@/components/TeamLink";
import { useTeam } from "@/components/useTeam";
import { discoveryLinks, type CounterpartyReport } from "@/lib/trade-counterparty";
import styles from "./scouting.module.css";

/** Trade discovery links with this manager, for the analysed team (`useTeam`): hidden when this manager is that team. */
export function CounterpartyDiscovery({ report, roster }: {
  report: Pick<CounterpartyReport, "team" | "discovery">;
  roster: { position: string; league_comparison: string }[];
}) {
  const team = useTeam();
  const links = discoveryLinks(report, roster, team === null ? null : Number(team));
  if (links.length === 0) return null;
  return (
    <div className={styles.discovery}>
      <h3 className={styles.patternTitle}>Explore trades with this manager</h3>
      <p className={styles.patternMeta}>Opens trade discovery with this manager as the only partner. A position link targets players you would receive at that position.</p>
      <ul className={styles.links}>{links.map((l) => (
        <li key={l.href}><TeamLink className="chip" href={l.href}>{l.position ?? "Any position"}</TeamLink><span>{l.position ? l.basis : "Every position the search covers"}</span></li>
      ))}</ul>
    </div>
  );
}
