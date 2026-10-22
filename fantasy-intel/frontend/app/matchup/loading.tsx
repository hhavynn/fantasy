import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";

// Mirrors the Matchup layout: header with the week tabs, the scoreboard band, then the two lineups side by side.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading matchup…</VisuallyHidden>
      <PageHeader
        eyebrow={<Skeleton w={140} h={10} />}
        title={<Skeleton w={240} h={30} radius={4} />}
        meta={<Skeleton w={320} h={12} />}
        actions={<Skeleton w={240} h={34} />}
      >
        <Skeleton w="60%" h={30} />
      </PageHeader>
      <Skeleton h={132} radius={10} />
      <Section title={<Skeleton w={160} h={14} />} flush>
        <SkeletonTable rows={10} cols={6} />
      </Section>
    </div>
  );
}
