import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while the player search or a profile loads from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading players…</VisuallyHidden>
      <PageHeader title="Players" meta={<Skeleton w={360} h={12} />}>
        <Skeleton w="100%" h={44} radius={6} />
      </PageHeader>
      <Section title={<Skeleton w={150} h={14} />} flush>
        <SkeletonTable rows={8} cols={7} />
      </Section>
    </div>
  );
}
