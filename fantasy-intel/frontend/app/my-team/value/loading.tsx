import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Mirrors the Team value layout: header with window tabs, five stats, summary, two charts, tables.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading team value…</VisuallyHidden>
      <PageHeader
        title={<Skeleton w={200} h={30} radius={4} />}
        meta={<Skeleton w={320} h={12} />}
        actions={<Skeleton w={220} h={34} />}
      >
        <Skeleton w={300} h={32} />
      </PageHeader>
      <SkeletonStatStrip n={5} />
      <Section title={<Skeleton w={220} h={14} />}>
        <Skeleton h={14} w="80%" />
      </Section>
      <div className="grid grid-2">
        <Section title={<Skeleton w={200} h={14} />}>
          <Skeleton h={200} radius={6} />
        </Section>
        <Section title={<Skeleton w={160} h={14} />}>
          <Skeleton h={200} radius={6} />
        </Section>
      </div>
      <Section title={<Skeleton w={200} h={14} />} flush>
        <SkeletonTable rows={5} cols={4} />
      </Section>
      <Section title={<Skeleton w={220} h={14} />} flush>
        <SkeletonTable rows={8} cols={7} />
      </Section>
    </div>
  );
}
