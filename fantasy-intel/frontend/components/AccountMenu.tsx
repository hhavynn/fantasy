import Link from "@/components/LeagueLink";
import { signOut } from "@/app/login/actions";
import { authConfig } from "@/lib/auth-config";
import { currentUser } from "@/lib/supabase/server";

/**
 * Signed out: "Sign in", and "Sign up" beside it while this site offers sign-up (SIGNUPS_OPEN=1; the sign-up page
 * itself also asks the API, so a mismatch shows "not open yet" there rather than here, without a backend call on
 * every page). Signed in: the account menu with "Sign out". Nothing when sign-in is off.
 */
export async function AccountMenu() {
  const config = authConfig(process.env);
  if (!config) return null;
  const user = await currentUser();
  if (!user) {
    return (
      <div className="account-entry">
        <Link href="/login" prefetch={false} className="account-link">Sign in</Link>
        {config.signupsOpen ? <Link href="/signup" prefetch={false} className="account-signup">Sign up</Link> : null}
      </div>
    );
  }
  return (
    <details className="account-menu">
      <summary className="account-link" title={user.email ?? undefined}>Account</summary>
      <div className="account-pop" role="menu">
        {user.email ? <p className="faint account-email">{user.email}</p> : null}
        <Link role="menuitem" href="/leagues">Your leagues</Link>
        <Link role="menuitem" href="/account" prefetch={false}>Account settings</Link>
        <Link role="menuitem" href="/terms" prefetch={false}>Terms</Link>
        <Link role="menuitem" href="/privacy" prefetch={false}>Privacy</Link>
        <form action={signOut}><button type="submit" role="menuitem" className="account-signout">Sign out</button></form>
      </div>
    </details>
  );
}
