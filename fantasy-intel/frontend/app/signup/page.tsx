import Link from "@/components/LeagueLink";
import { connection } from "next/server";
import { redirect } from "next/navigation";
import { Notice, PageHeader, Section } from "@/components/ui";
import { authConfig, safeNext } from "@/lib/auth-config";
import { CALLBACK_PROBLEMS, authHref, parseCallbackProblem } from "@/lib/auth-flow";
import { signupGateNow } from "@/lib/email-link.server";
import { currentUser } from "@/lib/supabase/server";
import { EmailLinkForm } from "../login/EmailLinkForm";
import styles from "../login/auth.module.css";

export const metadata = { title: "Create account" };

type Params = { next?: string; error?: string };

/**
 * Create account (issue #270): the same passwordless email link as Sign in, but the only place that may create an
 * account, and only while both launch gates are open (lib/auth-flow.ts). Closed, it says so and sends nothing.
 */
export default async function SignupPage({ searchParams }: { searchParams: Promise<Params> }) {
  await connection();
  const params = await searchParams;
  const next = safeNext(params.next);
  const meta = "An account saves your team and pinned leagues, and lets you connect private ESPN leagues. Public ESPN leagues, Sleeper leagues and the demo league need no account.";
  if (!authConfig(process.env)) {
    return (
      <div className={styles.page}>
        <PageHeader eyebrow="Account" title="Create account" meta={meta} />
        <Notice tone="info" title="Accounts are not available here">Public leagues and the demo league need no account.</Notice>
      </div>
    );
  }
  if (await currentUser()) redirect(next);
  const problem = parseCallbackProblem(params.error);
  const gate = await signupGateNow();
  return (
    <div className={styles.page}>
      <PageHeader eyebrow="Account" title="Create account" meta={meta} />
      {problem ? (
        <Notice tone={problem === "closed" ? "caution" : "negative"} title={CALLBACK_PROBLEMS[problem].title}>
          {CALLBACK_PROBLEMS[problem].body}
        </Notice>
      ) : null}
      {gate.open ? (
        <Section title="Email link" subtitle="No password to choose or remember.">
          <div className={styles.form}>
            <ol className={styles.steps}>
              <li>Enter your email and we send you a one-time link.</li>
              <li>Open the link in this browser within an hour. It only works where you asked for it, and only once.</li>
              <li>Your account is ready when the link opens. Nothing is created before that.</li>
            </ol>
            <EmailLinkForm intent="signup" next={next} />
          </div>
        </Section>
      ) : (
        <Notice tone="info" title="Sign-ups are not open yet">
          New accounts cannot be created right now, so nothing here sends an email. If you already have an account,
          sign in. Public leagues and the demo league work without one.
          <span className={styles.links}>
            <Link href="/leagues/demo" prefetch={false}>Open the demo league</Link>
            <Link href="/leagues" prefetch={false}>Open a public league</Link>
          </span>
        </Notice>
      )}
      <div className={styles.switch}>
        <p>Already have an account?</p>
        <Link href={authHref("/login", next)} prefetch={false} className={styles.switchLink}>Sign in</Link>
      </div>
    </div>
  );
}
