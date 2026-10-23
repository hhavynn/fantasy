"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { useLeagueHref } from "@/components/useAppPathname";
import { MAX_SHORTLIST, shortlistHref, withPlayer } from "@/lib/compare";
import { PlayerSlot } from "./ComparePicker";
import styles from "./compare.module.css";

/**
 * The shortlist's "Add a player" box: the pair view's own search box, appending to `ids` in the URL (slot, team and
 * start pick kept). On a league page the navigation stays in that league (`/l/<lg>/compare`).
 */
export function ShortlistPicker({ ids, slot, start, team }: {
  ids: number[];
  slot: string | null;
  start: number | null;
  team: string | null;
}) {
  const router = useRouter();
  const leagueHref = useLeagueHref();
  const [pending, startTransition] = useTransition();
  const [note, setNote] = useState<string | null>(null);
  const full = ids.length >= MAX_SHORTLIST;

  const add = (id: number) => {
    if (ids.includes(id)) {
      setNote("That player is already on the shortlist.");
      return;
    }
    setNote(null);
    startTransition(() => router.push(leagueHref(shortlistHref({ ids: withPlayer(ids, id), slot, start, team }))));
  };

  return (
    <div className={styles.picker} aria-busy={pending || undefined}>
      <PlayerSlot
        key={ids.join(",")}
        selected={null}
        onPick={add}
        autoFocus={ids.length === 0}
        disabled={full}
        label={`Add a player (${ids.length} of ${MAX_SHORTLIST})`}
        placeholder={full ? "The shortlist is full: remove a player first" : "Search a player to add"}
      />
      {note ? <p className={styles.pickerNote} role="status">{note}</p> : null}
    </div>
  );
}
