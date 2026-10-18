import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Shown while the power rankings load from the backend.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading power rankings…</VisuallyHidden>
      <PageHeader eyebrow="League" title="Power rankings" meta={<Skeleton w={360} h={12} />} />
      <Skeleton w={280} h={40} radius={6} />
      <SkeletonStatStrip />
      <Section title={<Skeleton w={190} h={14} />} flush>
        <SkeletonTable rows={12} cols={7} />
      </Section>
    </div>
  );
}
