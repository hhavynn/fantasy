import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { Badge, PageHeader } from "@/components/ui";
import { api } from "@/lib/api";
import { cx } from "@/lib/cx";
import { label, num } from "@/lib/format";
import { insufficientCount, standoutTendencies, winShare } from "@/lib/manager-view";
import { ConfDot, DIMENSION_TITLES } from "@/lib/managers";
import { nextDir, parseSort, sortHref, sortRows, type SortState } from "@/lib/sort";
import type { Classification, League, Metric, Team } from "@/lib/types";
import styles from "./managers.module.css";

export const metadata = { title: "Managers" };

type ManagerCard = Team & {
  classifications: Record<string, Classification>;
  metrics: Record<string, Metric>;
  composition: Record<string, number>;
};

const SORTS = [
  { key: "standing", label: "Standing", defaultDir: "asc" },
  { key: "record", label: "Record", defaultDir: "desc" },
  { key: "pf", label: "Points for", defaultDir: "desc" },
  { key: "adds", label: "Adds", defaultDir: "desc" },
  { key: "trades", label: "Trades", defaultDir: "desc" },
] as const;
const DEFAULT: SortState = { key: "standing", dir: "asc" };

export default async function ManagersPage({ searchParams }: PageProps<"/managers">) {
  const sp = await searchParams;
  const res = await api<{ league: League; managers: ManagerCard[] }>("/api/managers");
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Manager tendencies" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const { managers, league } = res.data;
  const sort = parseSort(sp, SORTS.map((s) => s.key), DEFAULT);
  const order = Object.keys(DIMENSION_TITLES);
  const rows = sortRows(managers.map((m, i) => ({ m, standing: i + 1 })), ({ m, standing }) => {
    switch (sort.key) {
      case "record": return winShare(m);
      case "pf": return m.points_for;
      case "adds": return m.metrics.adds_total?.value;
      case "trades": return m.metrics.trade_count?.value;
      default: return standing; // the API returns managers in standing order
    }
  }, sort.dir).map((r) => r.m);
  // Your card first, then the chosen order.
  const ordered = [...rows.filter((m) => m.is_mine), ...rows.filter((m) => !m.is_mine)];

  return (
    <div className="stack">
      <PageHeader
        title="Manager tendencies"
        meta={
          <>
            Measured from {league.season} transactions and weekly rosters (through week {league.current_week}). Labels are league-relative;
            a card shows only the tendencies labelled high with enough history, and open a manager for every rule, league mean and sample size.
            Confidence: <ConfDot level="HIGH" withText /> <ConfDot level="MEDIUM" withText /> <ConfDot level="LOW" withText />
          </>
        }
      />
      <nav className={styles.sort} aria-label="Sort managers">
        <span className={styles.sortLabel}>Sort by</span>
        {SORTS.map((s) => {
          const active = sort.key === s.key;
          const dir = nextDir(sort, s.key, s.defaultDir);
          return (
            <Link key={s.key} href={sortHref(sp, s.key, dir)} scroll={false} prefetch={false} className={cx(styles.sortLink, active && styles.sortActive)} aria-current={active ? "true" : undefined}>
              {s.label}{active ? <span aria-hidden="true"> {sort.dir === "asc" ? "▲" : "▼"}</span> : null}
            </Link>
          );
        })}
      </nav>
      <div className={styles.grid}>
        {ordered.map((m) => <Card key={m.team_id} m={m} league={league} order={order} />)}
      </div>
    </div>
  );
}

function Card({ m, league, order }: { m: ManagerCard; league: League; order: string[] }) {
  const adds = m.metrics.adds_total?.value;
  const drops = m.metrics.drops_total?.value;
  const trades = m.metrics.trade_count?.value;
  const c = m.composition;
  const standouts = standoutTendencies(m.classifications, order, 4);
  const unsure = insufficientCount(m.classifications);
  const record = `${m.wins}–${m.losses}${m.ties ? `–${m.ties}` : ""}`;
  return (
    <Link href={`/managers/${m.team_id}`} className={cx(styles.card, m.is_mine && styles.mine)}>
      <div className={styles.cardHead}>
        <h2 className={styles.name}>{m.name}{m.is_mine && <> <Badge value="You" tone="info" /></>}</h2>
        <span className={styles.record} title="Record">{record}</span>
      </div>
      <p className={styles.owner}>{m.owner_names ?? "owner unknown"}{m.playoff_seed ? ` · seed ${m.playoff_seed}` : ""}</p>

      <dl className={styles.metrics}>
        <div><dt>Points for</dt><dd>{num(m.points_for, 1)}</dd></div>
        <div><dt>Adds / drops</dt><dd>{num(adds, 0)} / {num(drops, 0)}</dd></div>
        <div><dt>Trades</dt><dd>{num(trades, 0)}</dd></div>
      </dl>

      <p className={styles.roster} title="Current roster construction">
        QB {c.QB ?? 0} · RB {c.RB ?? 0} · WR {c.WR ?? 0} · TE {c.TE ?? 0} · K {c.K ?? 0} · D/ST {c["D/ST"] ?? 0}
        {league.is_faab ? ` · $${m.faab_spent ?? 0} FAAB spent` : ` · waiver #${m.waiver_rank ?? "?"}`}
      </p>

      <div className={styles.tendencies}>
        {standouts.length === 0 ? (
          <p className={styles.none}>No standout tendencies with enough history yet.</p>
        ) : (
          <ul className={styles.tendencyList}>
            {standouts.map((t) => (
              <li key={t.dimension}>
                <span className={styles.tendencyName}>{DIMENSION_TITLES[t.dimension] ?? t.dimension}</span>
                <span className={styles.tendencyEnd}>
                  <Badge value={label(t.label)} tone="info" title={`${t.rule} · n=${t.sample_size} (min ${t.min_sample})`} />
                  <ConfDot level={t.confidence} withText />
                </span>
              </li>
            ))}
          </ul>
        )}
        {unsure > 0 && <p className={styles.foot}>{unsure} tendenc{unsure === 1 ? "y" : "ies"} without enough history</p>}
      </div>
    </Link>
  );
}
