import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading strength of schedule…</VisuallyHidden>
      <PageHeader eyebrow="Players · Research" title="Strength of schedule" meta={<Skeleton w={320} h={12} />} />
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={10} cols={8} />
      </Section>
    </div>
  );
}
