import Link from "@/components/LeagueLink";
import { TeamLink } from "@/components/TeamLink";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import { EmptyState, Method, Notice, PageHeader, Section, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { ESPN_COMPARISON_UNAVAILABLE } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { plural } from "@/lib/plural";
import {
  biggestMovers, cardHref, focusTeam, ordinal, PATH, weightText, type PowerRankings, type PrTeam,
} from "@/lib/power-rankings";
import { isUnknownTeam, readTeamSelection, withTeamId } from "@/lib/team-perspective";
import { Board } from "./Board";
import { QUESTION, Unavailable } from "./Unavailable";
import { TeamCard } from "./TeamCard";
import styles from "./power.module.css";

export const metadata = { title: "Power rankings" };

// League Power Rankings: every roster ranked on a visible composite (lineup strength, depth and injury cover,
// replacement options, all-play), each component scaled within the league, with week-over-week movement the
// backend recomputes point-in-time. Not the standings (Playoff race) and not player rankings (Rankings).

export default async function PowerRankingsPage({ searchParams }: PageProps<"/power-rankings">) {
  const sp = await searchParams;
  const sel = readTeamSelection(sp);
  if (sel.kind === "invalid") {
    return (
      <div className="stack">
        <PageHeader eyebrow="League" title="Power rankings" meta={QUESTION} />
        <UnknownTeam path={PATH} sel={sel} />
      </div>
    );
  }
  const [res, profile] = await Promise.all([api<PowerRankings>(withTeamId("/api/power-rankings", sel)), getLeagueProfile()]);
  const espn = profile.providerProjections;
  if (!res.ok) {
    if (isUnknownTeam(res, sel)) {
      return (
        <div className="stack">
          <PageHeader eyebrow="League" title="Power rankings" meta={QUESTION} />
          <UnknownTeam path={PATH} sel={sel} detail={res.error} />
        </div>
      );
    }
    return <Unavailable error={res.error} kind={res.kind} status={res.status} />;
  }
  const d = res.data;
  const focus = focusTeam(d);

  return (
    <div className="stack">
      <PageHeader eyebrow={d.week ? `League · Week ${d.week}` : "League"} title="Power rankings" meta={QUESTION} />
      <TeamPerspective path={PATH} sp={sp} current={d.focus_team_id} />

      {d.empty ? (
        <Section>
          <EmptyState title="Nothing to rank yet" action={<TeamLink href="/playoff-race">See the standings</TeamLink>}>
            <p>{d.message ?? "No team has a complete lineup projection."}</p>
          </EmptyState>
        </Section>
      ) : (
        <>
          <Lead d={d} focus={focus} />
          <Notices d={d} espn={espn} />
          <Section
            title="League power rankings"
            subtitle={`Week ${d.week} · ${rankedCount(d)} ${plural(rankedCount(d), "team")} · each component scaled 0–100 within the league; weights under each heading`}
            flush
            footnote={<>Team names open a shareable card. Lineup strength, depth and replacement use {weeks(d.window?.comparable_weeks.length ?? 0, "remaining week")}; all-play uses {weeks(d.window?.all_play_periods.length ?? 0, "completed week")}.</>}
          >
            <Board d={d} focus={focus?.team_id ?? null} />
          </Section>
          {focus && (
            <Section
              title={focus.is_mine ? "Your team card" : "Team card"}
              actions={<Link href={cardHref(focus.team_id)}>Open the shareable card</Link>}
            >
              <div className={styles.cardStage}>
                <TeamCard d={d} team={focus} heading="h3" />
              </div>
            </Section>
          )}
          <PowerMethod d={d} espn={espn} />
        </>
      )}
    </div>
  );
}

const rankedCount = (d: PowerRankings) => d.teams.filter((t) => t.rank !== null).length;
const weeks = (n: number, word: string) => `${n} ${plural(n, word)}`;

/** The answer first: who is on top, who rose, who fell, and where the focus team sits. */
function Lead({ d, focus }: { d: PowerRankings; focus: PrTeam | null }) {
  const top = d.teams.find((t) => t.rank === 1) ?? null;
  const { rise, fall, steady } = biggestMovers(d.teams);
  const prev = d.previous.available;
  return (
    <StatStrip>
      <Stat label="No. 1" value={top?.name ?? "—"} note={top?.power_score !== null && top ? `Power score ${top.power_score.toFixed(1)}` : undefined} />
      <Stat label="Biggest rise"
        value={rise ? rise.name : "—"}
        note={rise ? `▲ ${rise.movement} · ${ordinal(rise.previous_rank ?? 0)} to ${ordinal(rise.rank ?? 0)}` : prev ? (steady ? "Every rank held" : "No team rose") : "Needs last week's ranks"} />
      <Stat label="Biggest fall"
        value={fall ? fall.name : "—"}
        note={fall ? `▼ ${Math.abs(fall.movement ?? 0)} · ${ordinal(fall.previous_rank ?? 0)} to ${ordinal(fall.rank ?? 0)}` : prev ? (steady ? "Every rank held" : "No team fell") : "Needs last week's ranks"} />
      {focus && (
        <Stat label={focus.is_mine ? "Your rank" : `${focus.abbrev ?? "Team"} rank`}
          value={focus.rank ? ordinal(focus.rank) : "—"}
          note={focus.power_score !== null ? `Power score ${focus.power_score.toFixed(1)}` : focus.unranked_reason ?? undefined} />
      )}
    </StatStrip>
  );
}

/** `espn` is false for a league whose provider has no projections: last week's edition needs a stored ESPN snapshot. */
function Notices({ d, espn }: { d: PowerRankings; espn: boolean }) {
  return (
    <>
      {!d.previous.available && !espn && (
        <Notice tone="info" title="No week-over-week movement for this league">
          Movement is not available for this league: last week&apos;s ranks are rebuilt from a stored provider projection
          snapshot from before kickoff, and this league&apos;s provider publishes no projections.
        </Notice>
      )}
      {!d.previous.available && espn && (
        <Notice tone="info" title="No week-over-week movement this week">
          Last week&apos;s ranks could not be rebuilt from what was known before its kickoff: {d.previous.reason}.
          Movement is shown only when the stored rosters for that week are provably the rosters at kickoff and an ESPN projection snapshot from before kickoff is stored; nothing is estimated.
        </Notice>
      )}
      {d.window?.note && <Notice tone="caution" title="Some weeks are left out">{d.window.note}</Notice>}
    </>
  );
}

function PowerMethod({ d, espn }: { d: PowerRankings; espn: boolean }) {
  const p = d.previous;
  return (
    <Method title="How power rankings are calculated">
      <div className={styles.method}>
        <p>
          Power rankings order every roster by how strong it is now, not by its record. Standings and playoff scenarios live
          on <TeamLink href="/playoff-race">Playoff race</TeamLink>; single players on <Link href="/rankings">Rankings</Link>.
        </p>
        <dl className={styles.methodList}>
          {d.components.map((c) => (
            <div key={c.key}>
              <dt>{c.label} <span className={styles.weight}>{weightText(d.weights[c.key])}</span></dt>
              <dd>{c.description}</dd>
            </div>
          ))}
        </dl>
        <ul>
          {d.method.map((m) => <li key={m}>{m}</li>)}
          <li>K and D/ST are left out of depth and replacement because every team can stream them from waivers{d.kdst_streaming ? "; lineup strength values them against that weekly stream" : ""}.</li>
        </ul>
        {p.available ? (
          <p className="faint">
            Last week (week {p.week}) used week {p.roster_week} rosters, the ESPN projection snapshot of {p.projection_snapshot}
            {p.projection_synced_at ? <> (synced {when(p.projection_synced_at)})</> : null} and all-play through week{" "}
            {p.all_play_periods.length ? p.all_play_periods[p.all_play_periods.length - 1] : "—"}.
          </p>
        ) : espn ? (
          <p className="faint">Last week&apos;s ranks are unavailable: {p.reason}.</p>
        ) : (
          <p className="faint">Last week&apos;s ranks are unavailable for this league. {ESPN_COMPARISON_UNAVAILABLE}</p>
        )}
        <p className="faint">
          {espn ? "ESPN projections" : "Fantasy Intel projections"} synced {when(d.freshness.projections_synced_at)}; league synced {when(d.freshness.league_synced_at)}.
        </p>
      </div>
    </Method>
  );
}
