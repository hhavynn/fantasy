import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import {
  EmptyState, Glossary, Method, Notice, PageHeader, Section, Verdict, VerdictFigure,
} from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { plural } from "@/lib/plural";
import {
  COMPARE_PAGE_SIZE, compareApiPath, compareHref, parseCompareFilters, verdictNotes, type CompareResponse, type CompareSource,
} from "@/lib/projection-compare";
import type { SearchParams } from "@/lib/sort";
import { CompareControls } from "./CompareControls";
import { CompareTable } from "./CompareTable";
import { Disagreements } from "./Disagreements";
import styles from "./compare.module.css";

export const metadata = { title: "Source comparison" };

const QUESTION = "Where do the provider's projections and Fantasy Intel's own disagree, and what is recorded behind the gap?";

/** One source: what it is, whether this league can have it, and why not when it has no values. */
function SourceItem({ s, providerName }: { s: CompareSource; providerName: string }) {
  const stamp = s.key === "provider" ? s.synced_at : s.run_created_at;
  return (
    <div className={styles.source}>
      <dt>
        <span className={styles.kind}>{s.kind === "provider" ? "Provider" : "Model"}</span>
        <span>{s.label}</span>
      </dt>
      <dd>
        {s.available ? (
          <>
            {s.description}. {s.ranked} {plural(s.ranked, "player")} ranked in this window
            {stamp ? <>; {s.key === "provider" ? "synced" : "run"} {when(stamp)}</> : null}.
            {s.key === "ours" && s.experimental ? " Some weeks are forecast further ahead than one week." : ""}
          </>
        ) : !s.eligible ? (
          <>Not eligible in this league: {s.reason}</>
        ) : (
          <>{s.reason ?? `No ${s.key === "provider" ? providerName : "Ours"} values in this window.`}</>
        )}
        {s.key === "ours" && s.available && s.note ? <> {s.note}</> : null}
      </dd>
    </div>
  );
}

export default async function SourceComparePage({ searchParams }: PageProps<"/rankings/compare">) {
  const sp = (await searchParams) as SearchParams;
  const filters = parseCompareFilters(sp);
  const [profile, res] = await Promise.all([getLeagueProfile(), api<CompareResponse>(compareApiPath(filters))]);

  if (!res.ok) {
    const notSynced = res.status === 503 && res.kind === "http" && !/no database yet/i.test(res.error);
    return (
      <div className="stack">
        <PageHeader title="Source comparison" meta={QUESTION} />
        {notSynced ? (
          <Section>
            <EmptyState title="Your league isn't synced yet" action={<Link href="/data">Check data status</Link>}>
              <p>The comparison needs your league&apos;s players, projections and current week. Run the sync, then reload this page.</p>
              <p className="faint">{res.error}</p>
            </EmptyState>
          </Section>
        ) : <BackendProblem error={res.error} kind={res.kind} status={res.status} />}
      </div>
    );
  }

  const data = res.data;
  const [provider, ours] = data.sources;
  const name = data.provider.name;
  const c = data.counts;
  const t = data.thresholds;
  const scopeWord = data.scope === "position" ? "position ranks" : "overall ranks";
  const spNoOffset: SearchParams = Object.fromEntries(Object.entries(sp).filter(([k]) => k !== "offset"));
  const from = c.shown === 0 || data.players.length === 0 ? 0 : filters.offset + 1;
  const to = filters.offset + data.players.length;
  const hasPrev = filters.offset > 0;
  const hasNext = filters.offset + data.players.length < c.shown;
  const bothAvailable = provider.available && ours.available;
  const notes = verdictNotes(data.show, c.matched);
  const explained = data.players.filter((r) => r.delta.large && r.explanation).length;

  return (
    <div className="stack">
      <PageHeader eyebrow={`Players · Research · Week ${data.week}`} title="Source comparison" meta={QUESTION}
        actions={<Link className="chip" href="/rankings">Rankings</Link>} />

      <CompareControls sp={sp} filters={filters} leagueAvailable={data.rosters_synced} />

      {bothAvailable ? (
        <Verdict tone={c.large > 0 ? "caution" : "info"}
          headline={c.large > 0
            ? `${c.large} ${plural(c.large, "player")} with a large gap between ${name} and Ours`
            : `No large gap between ${name} and Ours in this view`}
          figures={<>
            <VerdictFigure label="Compared" value={c.compared} note={notes.compared} />
            <VerdictFigure label="Ours higher" value={c.ours_higher} note={notes.large} />
            <VerdictFigure label={`${name} higher`} value={c.provider_higher} note={notes.large} />
          </>}>
          A large gap is {t.large_rank} or more {scopeWord} and at least {t.min_points_per_week.toFixed(1)} points a week, for a player
          either source projects {t.relevant_per_week.toFixed(1)} or more a week. {data.window.label}.
          {notes.scope ? <> {notes.scope}</> : null}
        </Verdict>
      ) : (
        <Notice tone="caution" title={!provider.available ? `${provider.eligible ? name : "Provider"} rankings are unavailable here` : "Ours is unavailable here"}>
          {!provider.available ? provider.reason : ours.reason} Every rank on the unavailable side is shown as unavailable, never as 0, and no gap is computed.
        </Notice>
      )}

      <Section title="Sources in this league" subtitle="Neither column is an expert consensus.">
        <dl className={styles.sources}>
          <SourceItem s={provider} providerName={name} />
          <SourceItem s={ours} providerName={name} />
        </dl>
      </Section>

      {explained > 0 && (
        <Section title="Large gaps: what is recorded"
          subtitle="The model's own stored drivers, the recorded forecast history and the next-week range. Nothing here says why the provider differs."
          flush>
          <Disagreements data={data} />
        </Section>
      )}

      <Section
        title={<>Players <span className="faint">{c.shown} {plural(c.shown, "player")}</span></>}
        subtitle={`${data.scope === "position" ? "Ranks within each position" : "Ranks across QB, RB, WR and TE"}, computed on the whole pool before any filter. ${data.window.label}.`}
        actions={data.players.length > 0 ? <span className="faint">{from}–{to} of {c.shown}</span> : undefined}
        flush={data.players.length > 0}
        footnote={<>
          Rank gap = {provider.available ? name : "provider"} rank minus Ours rank; positive means Ours ranks him higher. Ranges: {data.ranges.horizon},
          {" "}{data.ranges.version} point distribution, P10–P90 if he plays (80% nominal coverage). {data.ranges.rest_of_season.reason}
          {!data.ranges.available && data.ranges.reason ? ` No range in this view: ${data.ranges.reason}.` : ""}
        </>}
      >
        {data.players.length > 0 ? (
          <CompareTable data={data} sort={data.sort} sp={spNoOffset} />
        ) : c.pool === 0 ? (
          <EmptyState title="No players in the pool yet" action={<Link href="/data">Check data status</Link>}>
            Sync your league to load its player pool, then reload this page.
          </EmptyState>
        ) : (
          <EmptyState title="No players match these filters" action={<Link className="chip reset" href="/rankings/compare">Clear filters</Link>}>
            Try another position, roster status, spelling or the All players view.
          </EmptyState>
        )}
      </Section>

      {(hasPrev || hasNext) && (
        <nav className={styles.pager} aria-label="Comparison pages">
          {hasPrev && <Link className="chip" scroll={false} href={compareHref(sp, { offset: filters.offset - COMPARE_PAGE_SIZE > 0 ? String(filters.offset - COMPARE_PAGE_SIZE) : null })}>Previous</Link>}
          <span className="sub">{from}–{to} of {c.shown}</span>
          {hasNext && <Link className="chip" scroll={false} href={compareHref(sp, { offset: String(filters.offset + COMPARE_PAGE_SIZE) })}>Next</Link>}
        </nav>
      )}

      <Glossary terms={["Rank gap", "Ours", "Projected points", "Floor and ceiling", "ROS", "Bye", "—"]} providerProjections={profile.providerProjections} />
      <Method title="How the comparison works">
        <div className={styles.method}>
          <p>
            Each source is ranked on its own projected points for the window, in your league&apos;s scoring, over the whole player pool before the
            position, roster and name filters, exactly as on Rankings. Equal projections (to 0.01) share a rank, shown with a T-. All positions ranks
            QB, RB, WR and TE together on raw points; within position ranks each position separately.
          </p>
          <p>
            A gap is shown only when both sources rank the player. A player on a bye, without a projection, or in a league whose provider publishes no
            projections has no rank on that side, and the gap says which side is missing: a missing rank is never treated as 0.
          </p>
          <p>
            For a large gap the page lists the Ours model&apos;s own largest input contributions as stored with the run, the movement between the two
            newest stored forecasts of each source and what was recorded over the same period. These are recorded alongside the forecasts and are not
            shown to be the cause. The provider publishes no reasoning, so nothing is said about why it differs.
          </p>
          <p>
            Ranges come only from Fantasy Intel&apos;s point-distribution model for next week&apos;s projection (RB, WR and TE; quarterbacks are held back),
            conditional on the player appearing. They are never projected over the rest of the season.
          </p>
          {data.notes.length > 0 && <ul className={styles.notes}>{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
        </div>
      </Method>
    </div>
  );
}
