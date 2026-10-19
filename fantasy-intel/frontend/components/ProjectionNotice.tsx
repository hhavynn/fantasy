import { Badge, Notice } from "@/components/ui";
import { horizonCaution, modelStatusLine, showingOurs, type ProjectionOrigin, type ProjectionSourceDetail } from "@/lib/projection-source";

/** What the page's projections are when the switch is on "Ours"; nothing for the ESPN default. */
export function ProjectionNotice({ detail, className }: { detail: ProjectionSourceDetail | null | undefined; className?: string }) {
  if (!detail || detail.source !== "independent") return null;
  if (!showingOurs(detail)) {
    return <Notice tone="caution" title="Showing ESPN projections" className={className}>{detail.reason}</Notice>;
  }
  const c = detail.counts;
  const title = detail.governance?.is_fallback
    ? "Showing Fantasy Intel projections (fallback model)"
    : "Showing Fantasy Intel projections";
  return (
    <Notice tone="info" title={title} className={className}>
      {modelStatusLine(detail)} K and D/ST come from our kicker and D/ST models. Players marked{" "}
      <OriginTag origin="espn" ours /> keep ESPN&apos;s number: anyone our models can&apos;t project this week.
      {c && <> This week: {c.ours} from our model, {c.ours_zero} at 0 (bye or unavailable), {c.espn} from ESPN.</>}
      {detail.reason && <> {detail.reason}</>}
      {horizonCaution(detail) && <> {horizonCaution(detail)}</>}
    </Notice>
  );
}

/** Marks a number that is not from the independent model while the switch is on "Ours". */
export function OriginTag({ origin, ours, title = "No independent estimate for this player: ESPN's projection" }: {
  origin: ProjectionOrigin | null | undefined; ours: boolean; title?: string;
}) {
  if (!ours || origin !== "espn") return null;
  return <Badge value="ESPN" tone="" title={title} />;
}
