"use client";

import Image from "next/image";
import { useState } from "react";
import { cx } from "@/lib/cx";
import { initials, playerPhoto } from "@/lib/player-photo";
import styles from "./PlayerPhoto.module.css";

type Status = { src: string; status: "loaded" | "failed" };

/** A player's ESPN headshot (a D/ST's team logo) in a circle. Decorative: the name always sits beside it.
 * Initials show while it loads and stay when ESPN has no photo; they are removed once the image is in,
 * since headshots and logos are transparent PNGs. `inline` spaces it before a name in running text. */
export function PlayerPhoto({ id, name, size = 20, inline = false, className }: {
  id: number; name: string | null; size?: number; inline?: boolean; className?: string;
}) {
  const photo = playerPhoto(id, size * 2);
  const [state, setState] = useState<Status | null>(null);
  const status = photo && state?.src === photo.src ? state.status : "pending";
  return (
    <span
      className={cx(styles.photo, photo?.kind === "logo" && styles.logo, inline && styles.inline, className)}
      style={{ width: size, height: size, fontSize: Math.max(8, Math.round(size * 0.4)) }}
      aria-hidden="true"
    >
      {status !== "loaded" ? <span className={styles.initials}>{initials(name)}</span> : null}
      {photo && status !== "failed" ? (
        <Image
          className={styles.img} src={photo.src} alt="" width={size} height={size} unoptimized
          // next/image replays load for an image that finished before hydration, broken ones included.
          onLoad={(e) => {
            if ((e.target as HTMLImageElement).naturalWidth > 0) {
              setState((s) => (s?.src === photo.src && s.status === "failed" ? s : { src: photo.src, status: "loaded" }));
            }
          }}
          onError={() => setState({ src: photo.src, status: "failed" })}
        />
      ) : null}
    </span>
  );
}
