// DOM ids that tie a `TabList` tab to its `TabPanel`. A plain module (no "use client"), so server components can
// call it as well as the client tab list.

export function tabDomId(base: string, id: string): string {
  return `${base}-tab-${id}`;
}

export function tabPanelDomId(base: string): string {
  return `${base}-panel`;
}
