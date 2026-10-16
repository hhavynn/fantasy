import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while waiver advice loads from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading waivers…</VisuallyHidden>
      <PageHeader title="Waivers" meta={<Skeleton w={300} h={12} />} />
      <Section title={<Skeleton w={110} h={14} />}>
        <Skeleton w="100%" h={96} radius={8} />
      </Section>
      <Section title={<Skeleton w={150} h={14} />} flush>
        <SkeletonTable rows={8} cols={7} />
      </Section>
    </div>
  );
}
