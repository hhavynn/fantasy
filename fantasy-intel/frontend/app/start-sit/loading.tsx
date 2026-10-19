import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Mirrors the Start/Sit layout: header, verdict band (headline and four figures), moves, lineup table.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading start/sit…</VisuallyHidden>
      <PageHeader
        title={<Skeleton w={220} h={30} radius={4} />}
        meta={<Skeleton w={360} h={12} />}
        actions={<Skeleton w={260} h={34} />}
      />
      <Skeleton h={76} radius={10} />
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={150} h={14} />}>
        <Skeleton h={18} w="70%" />
      </Section>
      <Section title={<Skeleton w={200} h={14} />} flush>
        <SkeletonTable rows={9} cols={5} />
      </Section>
    </div>
  );
}
