"use client";

import { useSyncExternalStore } from "react";
import { formatStamp } from "@/lib/transactions";

const subscribe = () => () => {};

/** A backend UTC stamp in the viewer's own time zone, with the zone named. The server render (and the first client
 *  render, so hydration matches) shows UTC; the browser then switches to its locale and zone. */
export function LocalTime({ iso }: { iso: string }) {
  const onClient = useSyncExternalStore(subscribe, () => true, () => false);
  const utc = formatStamp(iso, "UTC", "en-US");
  const text = onClient ? formatStamp(iso) : utc;
  return <time dateTime={iso} title={`${utc} (stored in UTC)`}>{text}</time>;
}
