import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading weekly recap…</VisuallyHidden>
      <PageHeader title="Weekly recap" meta={<Skeleton w={320} h={12} />} />
      <SkeletonStatStrip n={4} />
      <div className="grid grid-2">
        <Section title={<Skeleton w={120} h={14} />}><Skeleton h={220} /></Section>
        <Section title={<Skeleton w={120} h={14} />}><Skeleton h={220} /></Section>
      </div>
      <Section title={<Skeleton w={180} h={14} />} flush>
        <SkeletonTable rows={12} cols={8} />
      </Section>
    </div>
  );
}
