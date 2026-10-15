import { PageHeader, Section, Skeleton, SkeletonTable, VisuallyHidden } from "@/components/ui";
import styles from "./builder.module.css";

// Shown while the analyzer loads rosters (and the evaluation when a trade is in the URL).
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading the trade analyzer…</VisuallyHidden>
      <PageHeader title="Trade analyzer" meta={<Skeleton w={360} h={12} />} />
      <Section title={<Skeleton w={120} h={14} />}>
        <div className={styles.sides}>
          {[0, 1].map((i) => (
            <div key={i} className={styles.side}>
              <Skeleton h={34} />
              <Skeleton h={34} />
              <SkeletonTable rows={6} cols={3} />
            </div>
          ))}
        </div>
      </Section>
    </div>
  );
}
