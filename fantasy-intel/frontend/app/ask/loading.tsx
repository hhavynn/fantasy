import { PageHeader, Section, Skeleton, VisuallyHidden } from "@/components/ui";

export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true" data-loading-fallback>
      <VisuallyHidden>Loading the answer…</VisuallyHidden>
      <PageHeader eyebrow="Players · Research" title="Ask" meta={<Skeleton w={320} h={12} />} />
      <Skeleton w="100%" h={84} />
      <Section title={<Skeleton w={160} h={14} />}>
        <Skeleton w="100%" h={120} />
      </Section>
    </div>
  );
}
