import { PageHeader, Section, Skeleton, SkeletonStatStrip, VisuallyHidden } from "@/components/ui";

// Shown while the availability timeline loads from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading availability timeline…</VisuallyHidden>
      <PageHeader eyebrow="My team" title="Availability" meta={<Skeleton w={360} h={12} />} />
      <Skeleton w="100%" h={36} radius={6} />
      <SkeletonStatStrip />
      <Section title={<Skeleton w={140} h={14} />}>
        <Skeleton w="100%" h={80} radius={8} />
      </Section>
      <Section title={<Skeleton w={150} h={14} />}>
        <Skeleton w="100%" h={240} radius={8} />
      </Section>
    </div>
  );
}
