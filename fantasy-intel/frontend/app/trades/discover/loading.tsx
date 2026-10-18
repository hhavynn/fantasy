import { PageHeader, Section, Skeleton, SkeletonStatStrip, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Discovery runs an exact lineup search, so the first load can take a couple of seconds.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Searching for trades…</VisuallyHidden>
      <PageHeader title="Trade discovery" meta={<Skeleton w={400} h={12} />} />
      <Skeleton h={34} />
      <SkeletonStatStrip n={4} />
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={8} cols={7} />
      </Section>
    </div>
  );
}
