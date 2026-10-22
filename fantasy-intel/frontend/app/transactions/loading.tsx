import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading transactions…</VisuallyHidden>
      <PageHeader eyebrow="League" title="Transactions" meta={<Skeleton w={320} h={12} />} />
      <Skeleton h={44} />
      <Section title={<Skeleton w={140} h={14} />} flush>
        <SkeletonTable rows={12} cols={5} />
      </Section>
    </div>
  );
}
