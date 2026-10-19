import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading projection performance…</VisuallyHidden>
      <PageHeader eyebrow="Players · Research" title="Projection performance" meta={<Skeleton w={420} h={12} />} />
      <SkeletonStatStrip n={5} />
      <Section title={<Skeleton w={120} h={14} />} flush>
        <SkeletonTable rows={4} cols={6} />
      </Section>
    </div>
  );
}
