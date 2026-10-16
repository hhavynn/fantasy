import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading data quality…</VisuallyHidden>
      <PageHeader title="Data quality" meta={<Skeleton w={360} h={12} />} />
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={8} cols={4} />
      </Section>
    </div>
  );
}
