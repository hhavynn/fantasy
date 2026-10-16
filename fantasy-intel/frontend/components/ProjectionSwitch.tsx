"use client";

import { useRouter } from "next/navigation";
import { type KeyboardEvent, useRef, useState, useTransition } from "react";
import { cx } from "@/lib/cx";
import { setProjectionSource } from "@/lib/projection-actions";
import { SWITCH_SCOPE, type ProjectionSource } from "@/lib/projection-source";
import styles from "./ProjectionSwitch.module.css";

const CHOICES: { value: ProjectionSource; label: string; title: string }[] = [
  { value: "espn", label: "ESPN", title: "ESPN's weekly projections (default)" },
  { value: "ours", label: "Ours", title: "Fantasy Intel's own model (research: QB validated vs baselines, RB/WR/TE experimental)" },
];

/** ESPN / Ours as one radio group. `labels` adds a caption (the menu drawer on phones). */
export function ProjectionSwitch({ value, labels = false, className }: {
  value: ProjectionSource; labels?: boolean; className?: string;
}) {
  const router = useRouter();
  const [current, setCurrent] = useState(value);
  const [pending, startTransition] = useTransition();
  const group = useRef<HTMLDivElement>(null);

  const select = (next: ProjectionSource) => {
    if (next === current) return;
    setCurrent(next);
    startTransition(async () => {
      await setProjectionSource(next);
      router.refresh();
    });
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const next = current === "espn" ? "ours" : "espn";
    select(next);
    group.current?.querySelector<HTMLElement>(`[data-choice="${next}"]`)?.focus();
  };

  return (
    <div
      ref={group}
      className={cx(styles.switch, labels && styles.labelled, pending && styles.pending, className)}
      role="radiogroup"
      aria-label="Projection source"
      aria-busy={pending || undefined}
      title={labels ? undefined : `Projections: ${SWITCH_SCOPE}`}
      onKeyDown={onKeyDown}
    >
      {!labels && <span className={styles.caption} aria-hidden="true">Proj</span>}
      {CHOICES.map((c) => (
        <button
          key={c.value}
          type="button"
          role="radio"
          aria-checked={c.value === current}
          aria-label={`${c.label} projections`}
          title={c.title}
          data-choice={c.value}
          tabIndex={c.value === current ? 0 : -1}
          className={cx(styles.option, c.value === "ours" && styles.ours)}
          onClick={() => select(c.value)}
        >
          {c.label}
        </button>
      ))}
    </div>
  );
}
