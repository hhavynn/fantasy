import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while the injury view loads from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading injury impact…</VisuallyHidden>
      <PageHeader eyebrow="My team" title="Injury impact" meta={<Skeleton w={360} h={12} />} />
      <Skeleton w="100%" h={36} radius={6} />
      <Skeleton w="100%" h={64} radius={10} />
      <Section title={<Skeleton w={140} h={14} />}>
        <Skeleton w="100%" h={120} radius={8} />
      </Section>
      <Skeleton w="100%" h={44} radius={6} />
      <Section title={<Skeleton w={150} h={14} />}>
        <SkeletonTable rows={6} cols={7} />
      </Section>
    </div>
  );
}
