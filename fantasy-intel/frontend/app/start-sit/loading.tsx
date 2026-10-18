import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Mirrors the Start/Sit layout: header, verdict band with five stats, moves, lineup table.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading start/sit…</VisuallyHidden>
      <PageHeader
        title={<Skeleton w={220} h={30} radius={4} />}
        meta={<Skeleton w={360} h={12} />}
        actions={<Skeleton w={260} h={34} />}
      />
      <Skeleton h={64} radius={10} />
      <SkeletonStatStrip n={5} />
      <Section title={<Skeleton w={150} h={14} />}>
        <Skeleton h={18} w="70%" />
      </Section>
      <Section title={<Skeleton w={200} h={14} />} flush>
        <SkeletonTable rows={9} cols={5} />
      </Section>
    </div>
  );
}
