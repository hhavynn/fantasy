import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading your players across leagues…</VisuallyHidden>
      <PageHeader eyebrow="Connections" title="Player exposure" meta={<Skeleton w={360} h={12} />} />
      <SkeletonStatStrip />
      <Section title={<Skeleton w={120} h={14} />} flush>
        <SkeletonTable rows={8} cols={7} />
      </Section>
    </div>
  );
}
