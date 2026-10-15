"use client";

import Image from "next/image";
import { useState } from "react";
import { cx } from "@/lib/cx";
import { initials, playerPhoto } from "@/lib/player-photo";
import styles from "./PlayerPhoto.module.css";

/** A player's ESPN headshot (a D/ST's team logo) in a circle. Decorative: the name always sits beside it.
 * Initials show underneath while it loads and stay when ESPN has no photo for the player. */
export function PlayerPhoto({ id, name, size = 20, className }: {
  id: number; name: string | null; size?: number; className?: string;
}) {
  const photo = playerPhoto(id, size * 2);
  const [failed, setFailed] = useState<string | null>(null);
  const show = photo && failed !== photo.src;
  return (
    <span
      className={cx(styles.photo, photo?.kind === "logo" && styles.logo, className)}
      style={{ width: size, height: size, fontSize: Math.max(8, Math.round(size * 0.4)) }}
      aria-hidden="true"
    >
      <span className={styles.initials}>{initials(name)}</span>
      {show ? (
        <Image
          className={styles.img} src={photo.src} alt="" width={size} height={size} unoptimized
          onError={() => setFailed(photo.src)}
        />
      ) : null}
    </span>
  );
}
