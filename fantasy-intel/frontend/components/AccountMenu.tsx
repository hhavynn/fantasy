import Link from "next/link";
import { signOut } from "@/app/login/actions";
import { authConfig } from "@/lib/auth-config";
import { currentUser } from "@/lib/supabase/server";

/** "Sign in" or the signed-in email with "Sign out"; nothing when sign-in is off. */
export async function AccountMenu() {
  if (!authConfig(process.env)) return null;
  const user = await currentUser();
  if (!user) {
    return <Link href="/login" prefetch={false} className="account-link">Sign in</Link>;
  }
  return (
    <details className="account-menu">
      <summary className="account-link" title={user.email ?? undefined}>Account</summary>
      <div className="account-pop" role="menu">
        {user.email ? <p className="faint account-email">{user.email}</p> : null}
        <Link role="menuitem" href="/leagues">Your leagues</Link>
        <form action={signOut}><button type="submit" role="menuitem" className="account-signout">Sign out</button></form>
      </div>
    </details>
  );
}
