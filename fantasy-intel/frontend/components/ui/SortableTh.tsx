import Link from "next/link";
import type { ReactNode } from "react";
import { cx } from "@/lib/cx";
import { nextDir, sortHref, type SearchParams, type SortDir, type SortState } from "@/lib/sort";
import styles from "./SortableTh.module.css";

/**
 * A column header that sorts the table by link: `?sort=<sortKey>&dir=…`, every other search
 * param preserved. Pair with `parseSort` + `sortRows` in the page. Clicking the active column
 * flips its direction; clicking another uses `defaultDir` (default: "desc" for right-aligned
 * numeric columns, "asc" for left-aligned text).
 */
export function SortableTh({ sortKey, label, current, searchParams, align = "left", title, defaultDir, param, className }: {
  sortKey: string;
  label: ReactNode;
  current: SortState;
  searchParams: SearchParams | undefined;
  align?: "left" | "right";
  title?: string;
  defaultDir?: SortDir;
  /** Query-param name when a page has more than one sortable table (see `sortParamNames`). */
  param?: string;
  className?: string;
}) {
  const active = current.key === sortKey;
  const target = nextDir(current, sortKey, defaultDir ?? (align === "right" ? "desc" : "asc"));
  const aria = active ? (current.dir === "asc" ? "ascending" : "descending") : undefined;
  return (
    <th scope="col" aria-sort={aria} title={title} className={cx(styles.th, align === "right" && styles.right, className)}>
      <Link
        href={sortHref(searchParams, sortKey, target, param)}
        scroll={false}
        className={cx(styles.link, active && styles.active)}
        prefetch={false}
      >
        <span>{label}</span>
        <span className={styles.arrow} aria-hidden="true">{active ? (current.dir === "asc" ? "▲" : "▼") : "↕"}</span>
      </Link>
    </th>
  );
}
