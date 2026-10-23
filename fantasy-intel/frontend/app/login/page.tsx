import Link from "@/components/LeagueLink";
import { connection } from "next/server";
import { redirect } from "next/navigation";
import { Notice, PageHeader, Section } from "@/components/ui";
import { authConfig, safeNext } from "@/lib/auth-config";
import { CALLBACK_PROBLEMS, authHref, parseCallbackProblem, sameSiteLocation } from "@/lib/auth-flow";
import { signupGateNow } from "@/lib/email-link.server";
import { currentUser } from "@/lib/supabase/server";
import { EmailLinkForm } from "./EmailLinkForm";
import styles from "./auth.module.css";

export const metadata = { title: "Sign in" };

type Params = { next?: string; error?: string };

export default async function LoginPage({ searchParams }: { searchParams: Promise<Params> }) {
  await connection();
  const params = await searchParams;
  const next = safeNext(params.next);
  if (!authConfig(process.env)) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Account" title="Sign in" />
        <Notice tone="info" title="Sign-in is not available">Public leagues need no account.</Notice>
      </div>
    );
  }
  if (await currentUser()) redirect(sameSiteLocation(next));
  const problem = parseCallbackProblem(params.error);
  const gate = await signupGateNow();
  return (
    <div className={styles.page}>
      <PageHeader eyebrow="Account" title="Sign in"
                  meta="Signing in saves your team and pinned leagues, and lets you connect private ESPN leagues. Public ESPN leagues, Sleeper leagues and the demo league need no account." />
      {problem ? (
        <Notice tone={problem === "closed" ? "caution" : "negative"} title={CALLBACK_PROBLEMS[problem].title}>
          {CALLBACK_PROBLEMS[problem].body}
        </Notice>
      ) : null}
      <Section title="Email link">
        <EmailLinkForm intent="signin" next={next} />
      </Section>
      <div className={styles.switch}>
        {gate.open ? (
          <>
            <p>New to Fantasy Intel? Create an account with the same kind of email link.</p>
            <Link href={authHref("/signup", next)} prefetch={false} className={styles.switchLink}>Create an account</Link>
          </>
        ) : (
          <>
            <p>
              New to Fantasy Intel? <Link href={authHref("/signup", next)} prefetch={false}>Sign-ups</Link> are not
              open yet. Public leagues and the demo league need no account.
            </p>
            <Link href="/leagues/demo" prefetch={false} className={styles.switchLink}>Open the demo league</Link>
          </>
        )}
      </div>
    </div>
  );
}
