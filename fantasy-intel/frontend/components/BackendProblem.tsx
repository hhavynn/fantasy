import Link from "next/link";
import type { ReactNode } from "react";
import { Notice } from "@/components/ui";
import { isFirstRun, type ApiErrorKind } from "@/lib/api-errors";
import styles from "./BackendProblem.module.css";

/** A terminal command block. Paths are relative to the repository's `fantasy-intel/` folder. */
function Commands({ children }: { children: string }) {
  return <pre className={styles.commands}><code>{children}</code></pre>;
}

const START_BACKEND = "cd backend\nuv run uvicorn app.main:app --reload";
const SYNC = "cd backend\nuv run python ../scripts/sync_nfl.py\nuv run python ../scripts/sync_espn.py";

function FirstRun() {
  return (
    <>
      <p className={styles.firstRun}>
        <b>First run?</b> Copy <code>.env.example</code> to <code>.env</code> and fill it in, then import your league. Both syncs are safe to repeat.
      </p>
      <Commands>{SYNC}</Commands>
    </>
  );
}

/**
 * First run: the backend is up but no league has been imported. A getting-started guide rather than an error.
 * It describes the one supported setup (the owner's ESPN league via .env and the sync scripts); nothing here
 * pretends a league can be connected from the browser.
 */
export function FirstRunGuide() {
  return (
    <section className={styles.guide} aria-labelledby="first-run-title">
      <p className={styles.guideEyebrow}>Getting started</p>
      <h2 id="first-run-title" className={styles.guideTitle}>Import your league to get started</h2>
      <p className={styles.guideLead}>
        Fantasy Intel analyzes one ESPN fantasy football league: who to start, who to add and drop, which trades help both lineups, and how the
        league is shaping up. No league data has been imported on this server yet.
      </p>
      <ol className={styles.steps}>
        <li>
          <b>Find your league id.</b> Open your league on fantasy.espn.com: the number after <code>leagueId=</code> in the address bar is the id.
        </li>
        <li>
          <b>Fill in <code>fantasy-intel/.env</code>.</b> Copy <code>.env.example</code>, then set <code>ESPN_LEAGUE_ID</code> and <code>ESPN_SEASON</code>.
          A private league also needs your <code>espn_s2</code> and <code>SWID</code> browser cookies, saved as <code>ESPN_S2</code> and <code>SWID</code> (the
          README shows where to find them); they are read from the environment only and never stored.
        </li>
        <li>
          <b>Import the data.</b> Both syncs are safe to repeat:
          <Commands>{SYNC}</Commands>
        </li>
        <li><b>Reload this page.</b> The dashboard, lineup advice, waivers and trades fill in from the imported league.</li>
      </ol>
      <p className={styles.guideNote}>
        Connecting other leagues from the browser (Sleeper, public ESPN leagues) is a prototype that is switched off unless this deployment enables it;
        Yahoo is not available yet. See <Link href="/leagues">League connections</Link>.
      </p>
    </section>
  );
}

/**
 * Explains a failed backend call and what to do about it, by `kind` (see `ApiErrorKind`).
 * Pass the fields of a failed `api()` result. Safe in server and client components.
 */
export function BackendProblem({ error, kind = "unknown", status }: {
  error: string;
  kind?: ApiErrorKind;
  status?: number;
}) {
  let title: string;
  let body: ReactNode;
  if (kind === "http" && isFirstRun(error)) return <FirstRunGuide />;

  if (kind === "offline") {
    title = "Can't reach the backend";
    body = (
      <>
        <p>The dashboard reads everything from the local API, and it isn&apos;t answering. Start it in a terminal, then reload this page.</p>
        <Commands>{START_BACKEND}</Commands>
        <p className={styles.detail}>{error}</p>
        <FirstRun />
      </>
    );
  } else if (kind === "schema") {
    title = "The database schema is out of date";
    body = (
      <>
        <p>{error}</p>
        <p>Run both syncs to migrate and rebuild, then reload this page:</p>
        <Commands>{SYNC}</Commands>
      </>
    );
  } else if (kind === "http") {
    title = status ? `The backend returned an error (${status})` : "The backend returned an error";
    body = (
      <>
        <p>{error}</p>
        {/busy/i.test(error) && <p>A sync usually holds the database for a few seconds. Reload shortly.</p>}
      </>
    );
  } else {
    title = "Something went wrong";
    body = <p>{error}</p>;
  }

  return <Notice tone="negative" title={title}>{body}</Notice>;
}
