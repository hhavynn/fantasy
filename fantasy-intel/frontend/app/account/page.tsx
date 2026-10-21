import Link from "next/link";
import { connection } from "next/server";
import { Notice, PageHeader, Section } from "@/components/ui";
import { authConfig } from "@/lib/auth-config";
import { currentUser } from "@/lib/supabase/server";
import { DeleteAccount } from "./DeleteAccount";

export const metadata = { title: "Account" };

export default async function AccountPage() {
  await connection();
  const user = authConfig(process.env) ? await currentUser() : null;
  if (!user) {
    return (
      <div className="stack">
        <PageHeader eyebrow="Account" title="Account settings" />
        <Notice tone="info" title="Not signed in">
          <Link href="/login?next=/account" prefetch={false}>Sign in</Link> to manage your account.
        </Notice>
      </div>
    );
  }
  return (
    <div className="stack">
      <PageHeader eyebrow="Account" title="Account settings" meta={user.email ?? undefined} />
      <Section title="Your data">
        <p>
          Your leagues, pins and team choices are on the <Link href="/leagues" prefetch={false}>Leagues</Link> page.
          What is kept, and for how long, is in the <Link href="/privacy" prefetch={false}>Privacy policy</Link>.
        </p>
      </Section>
      <Section title="Delete account">
        <DeleteAccount />
      </Section>
    </div>
  );
}
