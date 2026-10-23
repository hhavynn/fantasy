import Link from "@/components/LeagueLink";
import { Section } from "@/components/ui";
import { api } from "@/lib/api";
import { profileChange, profileMoversQuery } from "@/lib/profile-attribution";
import { VIEW_NAME, beforeAfterText, freshnessText, signedPts, totalsText, type MoversResponse } from "@/lib/projection-movers";
import { WhyItChanged } from "../movers/WhyItChanged";
import styles from "./players.module.css";

const TITLE = "Fantasy Intel forecast change";

/**
 * "Why it changed" on the profile (issue #225): this player's change between the two stored Fantasy Intel forecasts the
 * Projection movers page compares, split into measured parts and a residual. It reads the movers route only; a player
 * it cannot compare is said in words, never shown as no change.
 */
export async function ProjectionChange({ id }: { id: number }) {
  const res = await api<MoversResponse>(`/api/projections/movers?${profileMoversQuery(id)}`);
  const d = res.ok ? res.data : null;
  const c = profileChange(d, id);
  const open = <Link href={c.href} prefetch={false}>Open in Projection movers</Link>;
  if (c.kind === "unavailable") {
    return (
      <Section title={TITLE} subtitle={`${VIEW_NAME.ros}, the two latest stored forecasts`}>
        <p className={styles.note}>{c.text} {open}</p>
      </Section>
    );
  }
  const m = c.mover;
  return (
    <Section title={TITLE} subtitle={d ? freshnessText(d) : undefined}>
      <p className={styles.hint}>
        {totalsText(d?.window.weeks ?? [])}: {beforeAfterText(m)} ({signedPts(m.delta) ?? "—"} pts). {open}
      </p>
      <WhyItChanged m={m} />
    </Section>
  );
}
