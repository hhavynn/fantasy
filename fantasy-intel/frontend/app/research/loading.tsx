import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while a research view loads: header, filter row, chart beside the evidence panel, the table.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading research…</VisuallyHidden>
      <PageHeader title={<Skeleton w={260} h={30} radius={4} />} meta={<Skeleton w={380} h={12} />}>
        <Skeleton w={180} h={32} radius={6} />
      </PageHeader>
      <Skeleton w="100%" h={56} radius={10} />
      <div className="grid-main">
        <Section title={<Skeleton w={200} h={14} />}><Skeleton w="100%" h={420} radius={8} /></Section>
        <Section title={<Skeleton w={140} h={14} />}><Skeleton w="100%" h={240} radius={8} /></Section>
      </div>
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={8} cols={8} />
      </Section>
    </div>
  );
}
