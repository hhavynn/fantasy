import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while the league analyzer loads from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading the league analyzer…</VisuallyHidden>
      <PageHeader eyebrow="League" title="League analyzer" meta={<Skeleton w={360} h={12} />} />
      <Skeleton w={280} h={40} radius={6} />
      <Skeleton w={520} h={36} radius={6} />
      <Skeleton w={640} h={120} radius={8} />
      <Section title={<Skeleton w={190} h={14} />} flush>
        <SkeletonTable rows={10} cols={9} />
      </Section>
    </div>
  );
}
