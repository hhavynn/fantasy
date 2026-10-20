import Link from "next/link";
import { connection } from "next/server";
import { Notice, PageHeader } from "@/components/ui";
import { authConfig } from "@/lib/auth-config";
import { multileagueUiEnabled } from "@/lib/my-leagues";
import { currentUser } from "@/lib/supabase/server";
import { RedeemInvite } from "./RedeemInvite";

export const metadata = { title: "Join a league" };

export default async function InvitePage() {
  await connection();
  const on = multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED) && !!authConfig(process.env);
  const user = on ? await currentUser() : null;
  return (
    <div className="stack">
      <PageHeader eyebrow="Leagues" title="Join a league" />
      {!on ? (
        <Notice tone="info" title="Invites are not available in this deployment">Ask the league&apos;s admin for a public link instead.</Notice>
      ) : !user ? (
        <Notice tone="info" title="Sign in to join">
          An invite adds you to a private league as a viewer. <Link href="/login?next=/invite" prefetch={false}>Sign in</Link>,
          then open the invite link again.
        </Notice>
      ) : (
        <RedeemInvite />
      )}
    </div>
  );
}
