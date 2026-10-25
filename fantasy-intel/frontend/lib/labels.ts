// Words that depend on which provider a league comes from (ESPN or Sleeper) and which projections a page shows
// (ESPN's or Fantasy Intel's own, "Ours"). Pure functions so node --test covers them. For provider "espn" every
// helper returns the wording the site always used, so the classic ESPN site reads exactly as before.

import { NO_PROJECTIONS } from "./start-sit-helpers.ts";

export type Provider = "espn" | "sleeper";

/** Anything the backend may call a provider, as one of ours; unknown values are ESPN (the classic site). */
export function normalizeProvider(value: unknown): Provider {
  return typeof value === "string" && value.trim().toLowerCase() === "sleeper" ? "sleeper" : "espn";
}

/** The provider's display name: "ESPN" or "Sleeper". */
export function providerName(provider: Provider | string | null | undefined): "ESPN" | "Sleeper" {
  return normalizeProvider(provider) === "sleeper" ? "Sleeper" : "ESPN";
}

export type ProjectionName = "ESPN" | "Ours" | typeof NO_PROJECTIONS;

/**
 * The projection a source or a backend label means: "Ours" for Fantasy Intel's own model (the switch value "ours",
 * the API source "independent", or a label such as "Ours" / "Fantasy Intel's own projections …"), "Unavailable" when
 * the backend names none (the response holds no projected number, #254), else "ESPN". A source is never named over
 * numbers it did not supply.
 */
export function projectionName(sourceOrLabel: string | null | undefined): ProjectionName {
  const v = (sourceOrLabel ?? "").trim().toLowerCase();
  if (v === NO_PROJECTIONS.toLowerCase()) return NO_PROJECTIONS;
  if (v === "ours" || v === "independent" || v.startsWith("ours") || v.includes("fantasy intel") || v.includes("(ours)")) return "Ours";
  return "ESPN";
}

/**
 * A projection name as the word before a noun: "ESPN " / "Ours ", or nothing for "Unavailable", so copy reads
 * "no projection" and "projected weeks", never "no Unavailable projection".
 */
export function projectionWord(name: string): string {
  return name === NO_PROJECTIONS ? "" : `${name} `;
}

/** Ownership and percent-started numbers come from ESPN only. */
export function ownershipAvailable(provider: Provider | string | null | undefined): boolean {
  return normalizeProvider(provider) === "espn";
}

export const OWNERSHIP_UNAVAILABLE = "Ownership % is not available for Sleeper leagues.";
/** For a league whose provider publishes no projections: the ESPN-vs-Ours comparisons have nothing to compare. No "ESPN"
 * in the wording, since a Sleeper league's pages never mention ESPN. */
export const ESPN_COMPARISON_UNAVAILABLE = "This league's provider publishes no projections, so there is nothing to compare Ours with.";

/** "ESPN league 123" / "Sleeper league 123": a league with no stored name. */
export function unnamedLeague(provider: Provider | string | null | undefined, externalId: string): string {
  return `${providerName(provider)} league ${externalId}`;
}

/** A player without a stored name. */
export function unnamedPlayer(id: number | string): string {
  return `Player #${id}`;
}

/**
 * "<label> projections" for a response's projection-source label, or "no projections" when the backend names none
 * ("Unavailable": the response holds no projected number, e.g. a Sleeper league without a Fantasy Intel run). A
 * source is never named over numbers it did not supply.
 */
export function projectionsPhrase(label: string | null | undefined): string {
  return !label || label === NO_PROJECTIONS ? "no projections" : `${label} projections`;
}
