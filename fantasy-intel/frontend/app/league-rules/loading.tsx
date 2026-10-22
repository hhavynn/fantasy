import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while the league rules load from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading league rules…</VisuallyHidden>
      <PageHeader eyebrow="League" title="League rules" meta={<Skeleton w={360} h={12} />} />
      <Skeleton w={420} h={48} radius={6} />
      <SkeletonStatStrip />
      <Section title={<Skeleton w={150} h={14} />} flush>
        <SkeletonTable rows={6} cols={3} />
      </Section>
      <Section title={<Skeleton w={130} h={14} />} flush>
        <SkeletonTable rows={10} cols={4} />
      </Section>
    </div>
  );
}
