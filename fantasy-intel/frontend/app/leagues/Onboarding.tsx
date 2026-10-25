import Link from "@/components/LeagueLink";
import { Badge, Notice, VisuallyHidden } from "@/components/ui";
import { DEMO_LABEL } from "@/lib/demo-league";
import { providerName } from "@/lib/labels";
import { leaguePath } from "@/lib/league-scope";
import { leagueName, type MyLeague } from "@/lib/my-leagues";
import {
  capabilitySummary, HOSTS, hostHref, providerInfo, supportWords, syncProgress, type Host, type ProviderInfo, type StepState,
} from "@/lib/onboarding";
import { ProgressActions } from "./ProgressActions";
import styles from "./leagues.module.css";

// The connect journey's server-rendered parts (issue #167). What can be connected and how far a league's first sync
// got both come from the backend on every request; the steps live in the URL (lib/onboarding.ts).

const STEPS = ["Choose the host", "Enter the league", "First sync"] as const;

/** Where the visitor is: three numbered steps, the current one marked for assistive tech too. */
export function Stepper({ current }: { current: 1 | 2 | 3 }) {
  return (
    <ol className={styles.stepper} aria-label="Adding a league">
      {STEPS.map((label, i) => {
        const n = i + 1;
        const state = n < current ? "done" : n === current ? "current" : "todo";
        return (
          <li key={label} className={styles[`step-${state}`]} aria-current={n === current ? "step" : undefined}>
            <span className={styles.stepNumber} aria-hidden="true">{n}</span>
            <span>{label}{state === "done" ? <VisuallyHidden> (done)</VisuallyHidden> : null}</span>
          </li>
        );
      })}
    </ol>
  );
}

const HOST_BLURB: Record<Host, string> = {
  espn: "Public leagues by their address. A private league also needs an ESPN account that is in it (next step explains why).",
  sleeper: "Leagues by their address or id. Sleeper leagues are readable by id, so no sign-in or cookies are ever asked for.",
};

/**
 * Step 1: the two hosts, with what each can and cannot supply in this deployment, straight from GET /v2/providers
 * (the backend's capability registry). A host switched off here says why and offers no link.
 */
export function HostCards({ providers, chooseHref }: { providers: ProviderInfo[] | null; chooseHref: (host: Host) => string }) {
  return (
    <ul className={styles.cards}>
      {HOSTS.map((host) => {
        const info = providers ? providerInfo(providers, host) : null;
        const off = info !== null && !info.enabled;
        const sum = info && info.enabled ? capabilitySummary(info) : null;
        const name = providerName(host);
        return (
          <li key={host}>
            <article className={styles.card} aria-labelledby={`host-${host}`}>
              <div className={styles.cardHead}>
                <h3 id={`host-${host}`} className={styles.cardTitle}>{name}</h3>
                {info ? <Badge value={off ? "Unavailable here" : "Available"} tone={off ? "" : "good"} /> : null}
              </div>
              <p className={styles.desc}>{HOST_BLURB[host]}</p>
              {off ? (
                <p className={styles.reason}><span className={styles.reasonLabel}>Why unavailable:</span> {info.reason ?? "Switched off in this deployment."}</p>
              ) : null}
              {sum ? <Capabilities host={host} available={sum.available} limited={sum.limited} /> : null}
              {!providers ? <p className={styles.hint}>What {name} supports could not be loaded right now.</p> : null}
              {off ? null : <Link href={chooseHref(host)} prefetch={false} className={styles.linkButton}>Choose {name}</Link>}
            </article>
          </li>
        );
      })}
    </ul>
  );
}

function Capabilities({ host, available, limited }: { host: Host; available: ProviderInfo["capabilities"]; limited: ProviderInfo["capabilities"] }) {
  return (
    <div className={styles.caps}>
      {available.length > 0 ? (
        <p className={styles.plain}><b className={styles.capsLead}>Reads:</b> {available.map((c) => c.label.toLowerCase()).join(", ")}.</p>
      ) : null}
      {limited.length > 0 ? (
        <details className={styles.details}>
          <summary>{limited.length === 1 ? "1 limit" : `${limited.length} limits`}</summary>
          <ul className={styles.capList}>
            {limited.map((c) => (
              <li key={c.name}><b>{c.label}</b>: {supportWords(c.support, host)}.{c.note ? <span className={styles.detailLine}>{c.note}</span> : null}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/** Before the cookie form: why ESPN private leagues are special, and the ways to avoid sharing cookies at all. */
export function EspnPrivateExplainer({ league, season }: { league: string; season: number }) {
  return (
    <Notice tone="info" title={`ESPN says league ${league} (${season}) is private`}>
      <p className={styles.problemText}>
        ESPN offers no sign-in that other sites can use. To read a private league, the sync job needs the <code>espn_s2</code> and{" "}
        <code>SWID</code> cookies of an ESPN account that is in the league: your own, never someone else&apos;s.
      </p>
      <p className={styles.problemText}>
        They are checked once with ESPN, stored encrypted (this website cannot read them back), used only to read this league, and
        you can remove them at any time in the league&apos;s settings. Signing out of ESPN everywhere stops them working.
      </p>
      <p className={styles.problemText}>
        Rather not? Ask a member who already added the league for an invite link, or ask the league manager to make the league
        viewable by the public on ESPN, then <Link href={hostHref("espn")} prefetch={false}>add it without cookies</Link>.
      </p>
    </Notice>
  );
}

const STEP_WORD: Record<StepState, string> = { done: "Done", current: "In progress", waiting: "Waiting", failed: "Stopped", todo: "Not yet" };

/** Step 3: the league's first sync as plain steps, the next action, and the dashboard link once it can open. */
export function SyncProgress({ league, note }: { league: MyLeague; note: "already" | "joined" | null }) {
  const p = syncProgress(league);
  const name = leagueName(league);
  return (
    <div className={styles.progress}>
      {note === "already" ? (
        <Notice tone="info" title="You already follow this league">Nothing was added twice. Here is where its sync stands.</Notice>
      ) : note === "joined" ? (
        <Notice tone="positive" title="Joined">This league already syncs with another member&apos;s cookies, so yours were not stored.</Notice>
      ) : null}
      <div className={styles.cardHead}>
        <h3 className={styles.cardTitle}>{name}</h3>
        <div className={styles.badges}>
          <Badge value={providerName(league.provider)} tone="" />
          <Badge value={`Season ${league.season}`} tone="" />
          <Badge value={league.visibility === "private" ? "Private" : "Public"} tone="" />
        </div>
      </div>
      <p className={styles.progressHeadline} role="status">{p.headline}</p>
      <ol className={styles.progressSteps}>
        {p.steps.map((s) => (
          <li key={s.label} className={styles[`ps-${s.state}`]}>
            <span className={styles.psState}>{STEP_WORD[s.state]}</span>
            <span className={styles.psLabel}>{s.label}</span>
            {s.detail ? <span className={styles.detailLine}>{s.detail}</span> : null}
          </li>
        ))}
      </ol>
      <div className={styles.actions}>
        {p.ready ? (
          <Link href={leaguePath(league.id, "/")} prefetch={false} className={styles.linkButton}>Open your dashboard</Link>
        ) : null}
        {p.actions.includes("cookies") ? (
          <Link href={`/leagues/${league.id}`} prefetch={false} className={styles.linkButtonSecondary}>Store fresh ESPN cookies</Link>
        ) : null}
        <Link href="/leagues" prefetch={false} className={styles.linkButtonSecondary}>Add another league</Link>
      </div>
      {p.actions.includes("invite") ? (
        <p className={styles.plain}>Not in this league on ESPN yourself? Ask a member for an invite link from the league&apos;s settings.</p>
      ) : null}
      {p.ready ? null : (
        <ProgressActions leagueId={league.id} polling={p.polling} canRetry={p.actions.includes("refresh")}
                         pinned={league.pinned} member={league.role !== null} />
      )}
    </div>
  );
}

/** The demo, offered before anything is connected. */
export function DemoEntry() {
  return (
    <article className={styles.card} aria-labelledby="demo-entry">
      <div className={styles.cardHead}>
        <h3 id="demo-entry" className={styles.cardTitle}>Look around a demo league first</h3>
        <Badge value={DEMO_LABEL} tone="" />
      </div>
      <p className={styles.desc}>
        A made-up four-team league with this week&apos;s lineup call, the waiver wire and the standings, worked out by the same engines a
        real league gets. No account needed.
      </p>
      <Link href="/leagues/demo" prefetch={false} className={styles.linkButton}>Open the demo league</Link>
    </article>
  );
}
