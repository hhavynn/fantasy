"use client";

import { usePathname } from "next/navigation";
import { type ReactNode, useCallback, useRef, useState } from "react";
import type { SyncState } from "@/lib/sync-status";
import { useDismiss } from "./useDismiss";

/** The pill button and its details popover; the details are rendered on the server and passed in. */
export function SyncPopover({ state, text, children }: { state: SyncState; text: string; children: ReactNode }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [shownFor, setShownFor] = useState(pathname);
  if (shownFor !== pathname) {
    setShownFor(pathname);
    setOpen(false);
  }
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root, trigger);

  return (
    <div className="sync" ref={root}>
      <button ref={trigger} type="button" className="sync-pill" data-state={state} aria-expanded={open}
              aria-controls="sync-pop" aria-haspopup="dialog" onClick={() => setOpen((o) => !o)}>
        {text}
      </button>
      {open && (
        <div id="sync-pop" className="sync-pop" role="dialog" aria-label="Data freshness">
          {children}
        </div>
      )}
    </div>
  );
}
