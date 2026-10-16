import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading kicker projections…</VisuallyHidden>
      <PageHeader eyebrow="Projections" title="Kicker outlook" meta={<Skeleton w={420} h={12} />} />
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={8} cols={7} />
      </Section>
    </div>
  );
}
