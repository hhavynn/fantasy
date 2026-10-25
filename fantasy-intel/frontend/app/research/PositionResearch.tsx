import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { EmptyState, Notice, PageHeader, Section, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import {
  POSITION_NAME, POSITION_OF, SLUGS, SORT_KEYS, WORKLOAD_MAPS, apiQuery, hiddenSentence, noSeasonData, parseResearchState,
  poolSentence,
  researchHref, type ResearchState, type Slug,
} from "@/lib/research";
import type { OpportunityResponse, PlayerResearch } from "@/lib/research-types";
import { parseSort } from "@/lib/sort";
import { MomentumChart, OpportunityChart, WorkloadChart } from "./charts";
import { Filters, ResearchMethod, YToggle } from "./Controls";
import { Compare, EvidencePanel, Timeline } from "./Evidence";
import { PlayerPicker } from "./PlayerPicker";
import { Leaderboard, ResearchGroups, ScoringArea } from "./Tables";
import styles from "./research.module.css";

type Params = Record<string, string | string[] | undefined>;

/** One position's research page. Every view, filter and selection is in the URL. */
export async function PositionResearch({ slug, searchParams }: { slug: Slug; searchParams: Promise<Params> }) {
  const sp = await searchParams;
  const state = parseResearchState(sp);
  const pos = POSITION_OF[slug];
  const [res, sel, cmp] = await Promise.all([
    api<OpportunityResponse>(`/api/research/opportunity?${apiQuery(pos, state)}`),
    state.sel ? api<PlayerResearch>(`/api/research/players/${encodeURIComponent(state.sel)}`) : null,
    state.sel && state.cmp ? api<PlayerResearch>(`/api/research/players/${encodeURIComponent(state.cmp)}`) : null,
  ]);
  const title = `${POSITION_NAME[pos]} research`;
  const tabs = <PositionTabs slug={slug} state={state} />;
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title={title} actions={<Link href="/research">Lab overview</Link>}>{tabs}</PageHeader>
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const r = res.data;
  const d = r.data;
  const sort = parseSort(sp, SORT_KEYS, { key: "xfp_pg", dir: "desc" });
  const selected = sel?.ok ? sel.data : null;
  const row = selected ? r.players.find((p) => p.gsis_id === selected.player.gsis_id) ?? null : null;
  const options = r.players.map((p) => ({ id: p.gsis_id, label: `${p.name} · ${p.team ?? "—"}`, href: researchHref(slug, state, { sel: p.gsis_id }) }));
  const cmpOptions = selected ? r.players.filter((p) => p.gsis_id !== state.sel)
    .map((p) => ({ id: p.gsis_id, label: `${p.name} · ${p.team ?? "—"}`, href: researchHref(slug, state, { cmp: p.gsis_id }) })) : [];

  return (
    <div className="stack">
      <PageHeader
        eyebrow="Players · Research lab"
        title={title}
        meta={d ? (
          <>
            {r.season} season · nflverse data through week {d.latest_week ?? "—"}
            {d.xfp_through_week !== d.latest_week && <> (xFP through week {d.xfp_through_week ?? "—"})</>}
            {" "}· full PPR · NFL sync {when(d.nfl_synced_at)}
          </>
        ) : "No NFL usage data yet"}
        actions={<Link href="/research">Lab overview</Link>}
      >
        {tabs}
      </PageHeader>

      {r.status === "no_data" ? (
        <Section><EmptyState title="No NFL usage data">{r.message}</EmptyState></Section>
      ) : noSeasonData(r) ? (
        <Section><EmptyState title="No NFL usage data">No NFL usage data is available for the {r.season} season yet.</EmptyState></Section>
      ) : (
        <>
          <DataNotices r={r} />
          <Filters slug={slug} r={r} state={state} />
          <p className={styles.pool}>{poolSentence(r)}. {hiddenSentence(r.pool.excluded)}</p>

          {r.status === "empty" ? (
            <Section>
              <EmptyState title={`No ${pos}s match these filters`} action={<Link href={`/research/${slug}`}>Reset filters</Link>}>
                Lower the games or volume filter, or choose another team.
              </EmptyState>
            </Section>
          ) : (
            <>
              <Section title="Worth investigating" footnote="Exploratory lists from the rules shown, not recommendations. Open a player for the evidence behind the call.">
                <ResearchGroups slug={slug} r={r} state={state} />
              </Section>

              <div className={styles.labGrid}>
                <Section
                  title={<ViewTitle state={state} pos={pos} />}
                  actions={state.view === "opportunity" ? <YToggle slug={slug} state={state} /> : undefined}
                  className={styles.chartSection}
                >
                  <ViewTabs slug={slug} state={state} pos={pos} />
                  {state.view === "workload" ? <WorkloadChart slug={slug} r={r} state={state} />
                    : state.view === "momentum" ? <MomentumChart slug={slug} r={r} state={state} />
                      : <OpportunityChart slug={slug} r={r} state={state} />}
                </Section>
                <div className={styles.side}>
                  {selected ? (
                    <EvidencePanel slug={slug} d={selected} state={state} row={row} />
                  ) : (
                    <Section title="Player evidence" id="evidence">
                      {sel && !sel.ok && <Notice tone="caution">{sel.status === 404 ? "That player has no WR, RB or TE usage this season." : sel.error}</Notice>}
                      <p className={styles.noteTight}>Select a marker, a table row or search by name to see the numbers behind a player, the opportunity trend, context and next steps.</p>
                      <PlayerPicker options={options} label="Find a player" placeholder={`Search ${r.players.length} ${pos}s`} action="Select" />
                    </Section>
                  )}
                  {selected && (
                    <Section title="Compare">
                      <PlayerPicker options={cmpOptions} label={`Compare ${selected.player.name} with`} placeholder="Player name" action="Compare" />
                      <PlayerPicker options={options} label="Select another player" placeholder="Player name" action="Select" />
                    </Section>
                  )}
                </div>
              </div>

              {selected && cmp?.ok && <Compare slug={slug} a={selected} b={cmp.data} state={state} />}
              {selected && <Timeline d={selected} />}

              <Leaderboard slug={slug} r={r} state={state} sort={sort} />
              <ScoringArea slug={slug} r={r} state={state} />
            </>
          )}
          <ResearchMethod r={r} />
        </>
      )}
    </div>
  );
}

function PositionTabs({ slug, state }: { slug: Slug; state: ResearchState }) {
  const items = SLUGS.map((s) => ({
    href: researchHref(s, { ...state, sel: null, cmp: null, team: null, minVolume: null, sort: null, dir: null }),
    label: POSITION_OF[s],
  }));
  return <Tabs label="Position" items={items} current={items[SLUGS.indexOf(slug)].href} />;
}

const VIEW_LABEL = { opportunity: "Opportunity vs production", workload: "", momentum: "Role momentum" };
const VIEW_QUESTION = {
  opportunity: "Who has the biggest roles, and is production keeping up?",
  workload: "",
  momentum: "Whose role grew before the box score did?",
};

/** The section asks the chart's question; the tabs below name the charts. */
function ViewTitle({ state, pos }: { state: ResearchState; pos: keyof typeof WORKLOAD_MAPS }) {
  return <>{state.view === "workload" ? WORKLOAD_MAPS[pos].question : VIEW_QUESTION[state.view]}</>;
}

function ViewTabs({ slug, state, pos }: { slug: Slug; state: ResearchState; pos: keyof typeof WORKLOAD_MAPS }) {
  const items = (["opportunity", "workload", "momentum"] as const).map((v) => ({
    href: researchHref(slug, state, { view: v }),
    label: v === "workload" ? WORKLOAD_MAPS[pos].title : VIEW_LABEL[v],
  }));
  return <Tabs label="Chart view" items={items} current={researchHref(slug, state)} className={styles.viewTabs} />;
}

function DataNotices({ r }: { r: OpportunityResponse }) {
  const d = r.data;
  if (!d) return null;
  const lag = d.weeks_missing_xfp;
  return (
    <>
      {lag.length > 0 && (
        <Notice tone="caution" title={`xFP not yet published for week ${lag.join(", ")}`}>
          nflverse expected points usually trail the box score by a day or two. Games in those weeks are left out of every average until it arrives; nothing is filled with zero.
        </Notice>
      )}
    </>
  );
}
