import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading trade values…</VisuallyHidden>
      <PageHeader eyebrow="Trades" title="Trade values" meta={<Skeleton w={320} h={12} />} />
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={120} h={14} />} flush>
        <SkeletonTable rows={8} cols={6} />
      </Section>
    </div>
  );
}
