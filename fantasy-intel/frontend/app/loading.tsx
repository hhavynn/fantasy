import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while any route's server components fetch from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading…</VisuallyHidden>
      <PageHeader title={<Skeleton w={240} h={30} radius={4} />} meta={<Skeleton w={380} h={12} />} />
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={140} h={14} />} flush>
        <SkeletonTable rows={7} cols={6} />
      </Section>
    </div>
  );
}
