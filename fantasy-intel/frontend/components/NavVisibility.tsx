"use client";

import { createContext, useContext, useMemo, type ReactNode } from "react";
import { ALL_NAV_PAGES, visibleGroups, type NavGroup } from "@/lib/navigation";

// Whether the owner research pages are hidden from the nav, decided once in the root layout (a league page and a
// visitor who is not a platform admin). The pages themselves answer "not found" from the API either way.
const HideOwnerOnly = createContext(false);

export function NavVisibility({ hideOwnerOnly, children }: { hideOwnerOnly: boolean; children: ReactNode }) {
  return <HideOwnerOnly.Provider value={hideOwnerOnly}>{children}</HideOwnerOnly.Provider>;
}

/** The nav groups this visitor sees. */
export function useNavGroups(): NavGroup[] {
  const hide = useContext(HideOwnerOnly);
  return useMemo(() => visibleGroups(hide), [hide]);
}

/** The routes this visitor may not open: the owner research pages when they are hidden. */
export function useHiddenHrefs(): ReadonlySet<string> {
  const hide = useContext(HideOwnerOnly);
  return useMemo(() => new Set(hide ? ALL_NAV_PAGES.filter((p) => p.ownerOnly).map((p) => p.href) : []), [hide]);
}
