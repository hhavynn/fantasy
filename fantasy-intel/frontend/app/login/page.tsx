import { connection } from "next/server";
import { redirect } from "next/navigation";
import { Notice, PageHeader, Section } from "@/components/ui";
import { authConfig, safeNext } from "@/lib/auth-config";
import { currentUser } from "@/lib/supabase/server";
import { LoginForm } from "./LoginForm";

export const metadata = { title: "Sign in" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string; error?: string }> }) {
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
  if (await currentUser()) redirect(next);
  return (
    <div className="stack">
      <PageHeader eyebrow="Account" title="Sign in"
                  meta="Signing in saves your team and pinned leagues, and lets you connect private ESPN leagues. Public leagues need no account." />
      {params.error ? <Notice tone="negative" title="That sign-in link did not work">Links work once, in the browser that asked for them, for one hour. Ask for a new one.</Notice> : null}
      <Section title="Email link">
        <LoginForm next={next} />
      </Section>
    </div>
  );
}
