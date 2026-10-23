import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading the player screener…</VisuallyHidden>
      <PageHeader eyebrow="Players · Research" title="Player screener" meta={<Skeleton w={320} h={12} />} />
      <Section title={<Skeleton w={160} h={14} />}>
        <Skeleton w="100%" h={96} />
      </Section>
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={120} h={14} />} flush>
        <SkeletonTable rows={10} cols={8} />
      </Section>
    </div>
  );
}
