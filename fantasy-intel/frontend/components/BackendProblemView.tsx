import Link from "@/components/LeagueLink";
import { Notice } from "@/components/ui";
import { SYNC, backendProblemCopy, type ProblemBlock, type ProblemInput } from "@/lib/backend-problem";
import styles from "./BackendProblem.module.css";

// Client-safe: no environment reads. Without `local` (a client component such as app/error.tsx, or a hosted
// deployment) it shows visitor wording only; pages render it through the server wrapper in ./BackendProblem.tsx,
// which sets `local` from the server's own environment.

/** A terminal command block. Paths are relative to the repository's `fantasy-intel/` folder. */
function Commands({ children }: { children: string }) {
  return <pre className={styles.commands}><code>{children}</code></pre>;
}

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

function Block({ block }: { block: ProblemBlock }) {
  if (block.type === "commands") return <Commands>{block.text}</Commands>;
  if (block.type === "detail") return <p className={styles.detail}>{block.text}</p>;
  if (block.type === "first-run") return <FirstRun />;
  return <p>{block.text}</p>;
}

/**
 * Explains a failed backend call and what to do about it, by `kind` (see `ApiErrorKind` and lib/backend-problem.ts).
 * Pass the fields of a failed `api()` result. `local` (developer help: backend text and commands) defaults to off.
 */
export function BackendProblemView(props: ProblemInput) {
  const copy = backendProblemCopy(props);
  if (copy.variant === "first-run") return <FirstRunGuide />;
  if (copy.variant === "first-sync") {
    // A league whose first sync has not published yet (the API's 409): a normal step of adding a league, not an error.
    return (
      <Notice tone="info" title={copy.title}>
        <p>{copy.detail}</p>
        <p><Link href="/leagues" prefetch={false}>Check sync status on Leagues</Link>, where you can refresh to start or retry a sync.</p>
      </Notice>
    );
  }
  return (
    <Notice tone="negative" title={copy.title}>
      {copy.blocks.map((b, i) => <Block key={i} block={b} />)}
    </Notice>
  );
}
