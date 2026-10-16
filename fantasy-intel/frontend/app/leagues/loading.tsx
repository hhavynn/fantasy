import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading leagues…</VisuallyHidden>
      <PageHeader title="Leagues" meta={<Skeleton w={360} h={12} />} />
      <Section title={<Skeleton w={160} h={14} />}>
        <Skeleton w="100%" h={96} />
      </Section>
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={3} cols={4} />
      </Section>
    </div>
  );
}
