import { Badge, Notice } from "@/components/ui";
import { showingOurs, type ProjectionOrigin, type ProjectionSourceDetail } from "@/lib/projection-source";

/** What the page's projections are when the switch is on "Ours"; nothing for the ESPN default. */
export function ProjectionNotice({ detail, className }: { detail: ProjectionSourceDetail | null | undefined; className?: string }) {
  if (!detail || detail.source !== "independent") return null;
  if (!showingOurs(detail)) {
    return <Notice tone="caution" title="Showing ESPN projections" className={className}>{detail.reason}</Notice>;
  }
  const c = detail.counts;
  const status = detail.model_status ?? {};
  return (
    <Notice tone="info" title="Showing Fantasy Intel projections (research model)" className={className}>
      Expected points from {detail.run?.model_version ?? "the independent model"}: QB {status.QB ?? "unknown"}, RB {status.RB ?? "unknown"},
      {" "}WR {status.WR ?? "unknown"}, TE {status.TE ?? "unknown"} (validated means against trailing-average baselines; nothing is
      validated against ESPN yet). Players marked <OriginTag origin="espn" ours /> keep ESPN&apos;s number: K, D/ST and anyone the model
      can&apos;t project.
      {c && <> This week: {c.ours} from our model, {c.ours_zero} at 0 (bye or unavailable), {c.espn} from ESPN.</>}
      {detail.reason && <> {detail.reason}</>}
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
