import { PageHeader, Skeleton, VisuallyHidden } from "@/components/ui";
import styles from "../power.module.css";

// Shown while a team card loads: the card's own footprint, so nothing shifts when it arrives.
export default function Loading() {
  return (
    <div className="stack" role="status" aria-busy="true">
      <VisuallyHidden>Loading team card…</VisuallyHidden>
      <PageHeader eyebrow="Power rankings" title="Team card" meta={<Skeleton w={320} h={12} />} />
      <div className={styles.cardStage}>
        <Skeleton w="100%" h={460} radius={14} />
      </div>
    </div>
  );
}
