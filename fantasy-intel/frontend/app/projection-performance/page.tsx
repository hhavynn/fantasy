import { BackendProblem } from "@/components/BackendProblem";
import { EmptyState, Notice, PageHeader, Section, Stat, StatStrip } from "@/components/ui";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import { plural } from "@/lib/plural";
import { POSITIONS, headline, weeksText, type PerformanceResponse } from "@/lib/projection-performance";
import { Coverage, EspnComparison, Methodology, ModelVersions, PositionCards, StartSit, WeeklyTrend } from "./sections";
import styles from "./performance.module.css";

export const metadata = { title: "Projection performance" };

const TITLE = "Projection performance";
const META = "Fantasy Intel's production projections against ESPN's pregame projections, scored only on fully final NFL weeks.";

function Governance({ g }: { g: PerformanceResponse["governance"] }) {
  if (g.espn_superiority_established) return null;
  return (
    <Notice tone="info" title={`${g.production_version} is production by owner override, not by evidence`}>
      Better accuracy than ESPN is not established{g.start_sit_ready ? "" : ", and no position is Start/Sit-ready"}. The override
      is a product decision; the figures below are the only accuracy evidence, and they are shown whichever way they point.
    </Notice>
  );
}

export default async function ProjectionPerformancePage() {
  const res = await api<PerformanceResponse>("/api/projections/performance");
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Projections" title={TITLE} />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const d = res.data;
  const r = d.report;
  const h = headline(d);

  if (!d.available || !r) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Projections" title={TITLE} meta={META} />
        <Notice tone={h.tone} title={h.title}>{h.body}</Notice>
        <Governance g={d.governance} />
        {d.rejected.length > 0 && (
          <Notice tone="caution" title={`${d.rejected.length} published ${plural(d.rejected.length, "report")} failed verification`}>
            {d.rejected.map((x) => x.reason).join("; ")}. Nothing from {d.rejected.length === 1 ? "it" : "them"} is shown.
          </Notice>
        )}
        <Section title="Current evidence status">
          <EmptyState title="No accuracy figures to show yet">
            The scorecard is built offline after each scheduled refresh and published with the database. Until a fully final
            week has paired ESPN and pregame Fantasy Intel projections, every position stays at insufficient prospective evidence.
          </EmptyState>
        </Section>
      </div>
    );
  }

  const paired = POSITIONS.reduce((a, p) => a + r.positions[p].paired_n, 0);
  const g = r.evidence_gate;
  const ss = r.start_sit;
  return (
    <div className="stack">
      <PageHeader eyebrow={`Projections · ${r.season}`} title={TITLE} meta={META} />

      {r.synthetic && (
        <Notice tone="caution" title="Synthetic test data">
          {r.label ?? "Invented data for tests and screenshots"}. None of these figures is a real result.
        </Notice>
      )}
      <Notice tone={h.tone} title={h.title}>{h.body}</Notice>
      <Governance g={d.governance} />
      {d.stale && (
        <Notice tone="caution" title="This report may be out of date">
          {d.stale_reasons.join("; ")}. It was built {when(r.generated_at)}; the next scheduled refresh rebuilds it.
        </Notice>
      )}
      {d.rejected.length > 0 && (
        <Notice tone="caution" title={`${d.rejected.length} newer ${plural(d.rejected.length, "report")} failed verification`}>
          {d.rejected.map((x) => x.reason).join("; ")}. The newest report that verifies is shown.
        </Notice>
      )}

      <StatStrip className={styles.strip}>
        <Stat label="Fully final weeks" value={r.weeks.fully_final.length}
          note={`${r.weeks.fully_final.length ? `week ${weeksText(r.weeks.fully_final)}` : "none yet"} · ${g.min_weeks} needed per position`} />
        <Stat label="In progress" value={r.weeks.in_progress.length}
          note={r.weeks.in_progress.length ? `week ${weeksText(r.weeks.in_progress)} · preliminary, no verdict`
            : r.weeks.upcoming.length ? `next: week ${weeksText(r.weeks.upcoming)}` : "no week in progress"} />
        <Stat label="Matched observations" value={paired}
          note={`all positions · ${g.min_matched_per_position} needed per position`} />
        <Stat label="Start/Sit decision sets" value={ss.sets}
          note={ss.sets ? `week ${weeksText(ss.weeks_recorded)} · ${g.start_sit_min_weeks} weeks needed` : "none recorded yet"} />
        <Stat label="Report built" value={when(r.generated_at)} tone={d.stale ? "caution" : undefined}
          note={d.stale ? "may be out of date" : "verified against its content hash"} />
      </StatStrip>

      <PositionCards r={r} />
      <EspnComparison r={r} />
      <WeeklyTrend r={r} />
      <ModelVersions r={r} />
      <StartSit r={r} />
      <Coverage r={r} />
      <Methodology r={r} sha={d.report_sha256} />
    </div>
  );
}
