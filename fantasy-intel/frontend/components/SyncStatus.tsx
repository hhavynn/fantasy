"use client";

import { useEffect, useRef, useState } from "react";
import { computeSyncSummary, type SyncSummary } from "@/lib/sync-status";
import type { SyncInfo } from "@/lib/types";

type SyncStatusProps = {
  status: {
    sync: SyncInfo;
    nfl_latest_week: number | null;
    league: { name: string; current_week: number } | null;
  } | null;
};

export function SyncStatus({ status }: SyncStatusProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const summary: SyncSummary = computeSyncSummary(status);

  useEffect(() => {
    if (!isOpen) return;

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setIsOpen(false);
        triggerRef.current?.focus();
      }
    }

    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handleClickOutside);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [isOpen]);

  const toggle = () => setIsOpen((prev) => !prev);

  return (
    <div className="sync-status-container" ref={containerRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`sync-trigger tone-${summary.tone} ${isOpen ? "active" : ""}`}
        onClick={toggle}
        aria-expanded={isOpen}
        aria-controls="sync-status-popover"
        aria-haspopup="dialog"
        aria-label={`${summary.label}: click for sync details`}
      >
        <span className={`sync-dot tone-${summary.tone}`} aria-hidden="true" />
        <span className="sync-trigger-label">{summary.label}</span>
        <svg
          className={`sync-chevron ${isOpen ? "rotated" : ""}`}
          width="12"
          height="12"
          viewBox="0 0 12 12"
          aria-hidden="true"
        >
          <path d="M2.5 4.5L6 8L9.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </button>

      {isOpen && (
        <div
          id="sync-status-popover"
          className="sync-popover"
          role="dialog"
          aria-label="Synchronization details"
        >
          <div className="sync-popover-header">
            <h4>Data Freshness</h4>
            <span className={`badge ${summary.tone === "neutral" ? "info" : summary.tone}`}>
              {summary.label}
            </span>
          </div>

          <div className="sync-popover-body">
            {summary.state === "offline" && (
              <div className="sync-callout bad">
                <b>Backend offline</b>
                <p>Cannot reach the local FastAPI server. Start it with:</p>
                <code>uv run uvicorn app.main:app --reload</code>
              </div>
            )}

            {summary.state === "not_synced" && (
              <div className="sync-callout warn">
                <b>No sync data</b>
                <p>Data has not yet been synchronized from ESPN or NFL sources.</p>
              </div>
            )}

            {summary.activeFailures.length > 0 && (
              <div className="sync-callout warn">
                <b>Active Sync Warning</b>
                {summary.activeFailures.map((failure, idx) => (
                  <p key={idx}>
                    <b>{failure.source.toUpperCase()}:</b> {failure.message ?? "Sync encountered an error."}
                  </p>
                ))}
              </div>
            )}

            <div className="sync-rows">
              <div className="sync-row">
                <span className="sync-source">ESPN League</span>
                <span className="sync-val">{summary.details.espn.formatted}</span>
              </div>
              <div className="sync-row">
                <span className="sync-source">NFL Stats & Rosters</span>
                <span className="sync-val">
                  {summary.details.nfl.formatted}
                  {summary.details.nfl.statsWeek !== null && (
                    <span className="faint"> (wk {summary.details.nfl.statsWeek})</span>
                  )}
                </span>
              </div>
              <div className="sync-row">
                <span className="sync-source">Analytics Engine</span>
                <span className="sync-val">{summary.details.analytics.formatted}</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
