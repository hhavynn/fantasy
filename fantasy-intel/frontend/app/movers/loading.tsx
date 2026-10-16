import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading projection movers…</VisuallyHidden>
      <PageHeader eyebrow="Projections" title="Projection movers" meta={<Skeleton w={320} h={12} />} />
      <SkeletonStatStrip n={3} />
      <Section title={<Skeleton w={120} h={14} />} flush>
        <SkeletonTable rows={5} cols={4} />
      </Section>
    </div>
  );
}
