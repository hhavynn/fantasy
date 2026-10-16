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
    : "Showing Fantasy Intel projections (not proven against ESPN)";
  return (
    <Notice tone="info" title={title} className={className}>
      {modelStatusLine(detail)} Players marked <OriginTag origin="espn" ours /> keep ESPN&apos;s number: K, D/ST and anyone the model
      can&apos;t project.
      {c && <> This week: {c.ours} from our model, {c.ours_zero} at 0 (bye or unavailable), {c.espn} from ESPN.</>}
      {detail.reason && <> {detail.reason}</>}
      {horizonCaution(detail) && <> <strong>Caution: {horizonCaution(detail)}</strong></>}
    </Notice>
  );
}

/** Marks a number that is not from the independent model while the switch is on "Ours". */
export function OriginTag({ origin, ours }: { origin: ProjectionOrigin | null | undefined; ours: boolean }) {
  if (!ours || origin !== "espn") return null;
  return <Badge value="ESPN" tone="" title="No independent estimate for this player: ESPN's projection" />;
}
