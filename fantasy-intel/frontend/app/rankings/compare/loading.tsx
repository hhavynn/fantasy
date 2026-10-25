import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while the comparison loads from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading the source comparison…</VisuallyHidden>
      <PageHeader title="Source comparison" meta={<Skeleton w={320} h={12} />} />
      <Skeleton w="100%" h={40} radius={6} />
      <Skeleton w="100%" h={96} radius={10} />
      <Section title={<Skeleton w={150} h={14} />} flush>
        <SkeletonTable rows={10} cols={9} />
      </Section>
    </div>
  );
}
