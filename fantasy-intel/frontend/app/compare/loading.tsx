import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Mirrors the comparison layout: header with the two pickers, identity cards, summary, a table.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading comparison…</VisuallyHidden>
      <PageHeader title="Compare players" meta={<Skeleton w={380} h={12} />}>
        <Skeleton w="100%" h={64} radius={6} />
      </PageHeader>
      <SkeletonStatStrip n={2} />
      <Section title={<Skeleton w={160} h={14} />}>
        <Skeleton h={18} w="75%" />
      </Section>
      <Section title={<Skeleton w={200} h={14} />} flush>
        <SkeletonTable rows={7} cols={4} />
      </Section>
    </div>
  );
}
