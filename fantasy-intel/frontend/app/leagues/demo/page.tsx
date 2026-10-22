import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge, Notice, PageHeader, Pos, Section, Verdict, VerdictFigure } from "@/components/ui";
import { DEMO_LABEL, parseDemoLeague, record } from "@/lib/demo-league";
import demoFixture from "@/lib/fixtures/demo-league.synthetic.json";
import { num } from "@/lib/format";
import { multileagueUiEnabled } from "@/lib/my-leagues";
import styles from "../leagues.module.css";

export const metadata = { title: "Demo league" };

/**
 * The demo a visitor can open before connecting anything (issue #167). It is the synthetic fixture league's recorded
 * engine output, bundled with the site (lib/demo-league.ts): no backend call, no account, no stored state, and no way
 * to show a real league. Every number came from the engines; this page only lays them out.
 */
export default function DemoLeague() {
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) notFound();
  const demo = parseDemoLeague(demoFixture);
  if (!demo) notFound();
  const { league, lineup } = demo;
  const upgrade = lineup.start.length > 0 && (lineup.projectedGain ?? 0) > 0;
  return (
    <div className="stack">
      <PageHeader eyebrow={DEMO_LABEL} title={league.name}
                  meta={`Season ${league.season} · week ${league.week} · ${league.teams} teams${league.receptionPoints ? ` · ${league.receptionPoints} point per reception` : ""}. You are viewing ${demo.team}.`}
                  actions={<Link href="/leagues" prefetch={false} className={styles.linkButton}>Connect your league</Link>} />
      <Notice tone="info" title="Everything on this page is invented">
        The teams, players and numbers come from the made-up league the site&apos;s own tests use, run through the same lineup,
        standings and waiver engines as a real league. Nothing here belongs to anyone, and nothing you do here is saved.
      </Notice>

      <Verdict tone={upgrade ? "positive" : "info"} eyebrow={`${demo.team} · week ${league.week} lineup`}
               headline={upgrade
                 ? `Start ${lineup.start.map((p) => p.name).join(" and ")} for ${num(lineup.projectedGain)} more projected points`
                 : "The current lineup is already the best one"}
               figures={<>
                 <VerdictFigure label="Projected gain" value={num(lineup.projectedGain)} emphasis tone={upgrade ? "positive" : undefined} />
                 <VerdictFigure label="Current lineup" value={num(lineup.currentTotal)} note="projected points" />
                 <VerdictFigure label="Best lineup" value={num(lineup.recommendedTotal)} note="projected points" />
               </>}>
        This is the first decision a connected league gets: the start/sit page finds the best legal lineup and compares it with the one you set.
      </Verdict>

      {upgrade ? (
        <Section title="The swap" subtitle="Projected points this week, under the league's own scoring." flush>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Move</th><th>Player</th><th>Pos</th><th>NFL team</th><th className="num">Projected</th></tr></thead>
              <tbody>
                {lineup.start.map((p) => (
                  <tr key={`in-${p.name}`}><td>Start</td><td>{p.name}</td><td><Pos value={p.position} /></td><td>{p.proTeam ?? "—"}</td><td className="num">{num(p.projection)}</td></tr>
                ))}
                {lineup.bench.map((p) => (
                  <tr key={`out-${p.name}`}><td>Bench</td><td>{p.name}</td><td><Pos value={p.position} /></td><td>{p.proTeam ?? "—"}</td><td className="num">{num(p.projection)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      <Section title="Waiver wire" subtitle="The free agents the waiver engine looked at first, with its call and its main reason." flush>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Player</th><th>Pos</th><th>Call</th><th className="num">Score</th><th>Why</th></tr></thead>
            <tbody>
              {demo.waivers.map((p) => (
                <tr key={p.name}>
                  <td>{p.name}</td><td><Pos value={p.position} /></td><td><Badge value={p.recommendation} /></td>
                  <td className="num">{num(p.waiverScore, 2)}</td><td>{p.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Standings" subtitle="Records and points as the league reports them." flush>
        <div className="table-wrap">
          <table>
            <thead><tr><th className="num">Seed</th><th>Team</th><th className="num">Record</th><th className="num">Points for</th><th className="num">Points against</th></tr></thead>
            <tbody>
              {demo.standings.map((t) => (
                <tr key={t.team} aria-current={t.mine ? "true" : undefined}>
                  <td className="num">{t.seed ?? "—"}</td><td>{t.team}{t.mine ? " (you)" : ""}</td><td className="num">{record(t)}</td>
                  <td className="num">{num(t.pointsFor)}</td><td className="num">{num(t.pointsAgainst)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Your own league" subtitle="ESPN or Sleeper, in three steps: choose the host, paste the league's address, wait for the first sync.">
        <p className={styles.plain}>
          A connected league gets every page of the dashboard: lineup, waivers, trades, matchups and the league&apos;s race.{" "}
          <Link href="/leagues" prefetch={false}>Connect a league</Link>.
        </p>
      </Section>
    </div>
  );
}
