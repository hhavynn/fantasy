import Link from "next/link";
import { PageHeader, Section } from "@/components/ui";
import styles from "../terms/legal.module.css";

export const metadata = { title: "Privacy policy" };

const ISSUES = "https://github.com/hhavynn/fantasy/issues";

export default function PrivacyPage() {
  return (
    <div className="stack">
      <PageHeader eyebrow="About" title="Privacy policy" meta="Last updated: October 20, 2026" />
      <Section title="What is stored">
        <div className={styles.prose}>
          <ul>
            <li>Your email address, to sign you in.</li>
            <li>The ids of the leagues you add, pin or are invited to.</li>
            <li>Your pinned leagues and the team you pick in each league.</li>
            <li>For a private ESPN league, your ESPN cookies, encrypted (AES-256-GCM, with the key wrapped by RSA-OAEP).
              They are used only to read that league and are never shown back.</li>
            <li>An audit log of account actions, such as connecting a league or sending an invite. It holds metadata
              only and is kept for 1 year.</li>
          </ul>
          <p>Viewing a public league needs no account.</p>
        </div>
      </Section>
      <Section title="Who processes it">
        <div className={styles.prose}>
          <ul>
            <li>Supabase: sign-in and the database.</li>
            <li>Cloudflare R2: league data files.</li>
            <li>Vercel: hosting.</li>
            <li>GitHub Actions: the jobs that sync league data.</li>
          </ul>
        </div>
      </Section>
      <Section title="What is not done">
        <div className={styles.prose}>
          <p>Your data is never sold. The site shows no ads and uses no third-party tracking.</p>
        </div>
      </Section>
      <Section title="Deleting your data">
        <div className={styles.prose}>
          <p>Delete your account from <Link href="/account" prefetch={false}>Account settings</Link> with Delete account.</p>
          <ul>
            <li>Your ESPN cookies are crypto-erased immediately.</li>
            <li>Your league memberships and pins are removed.</li>
            <li>A private league with no members left is deleted, and its data files are removed 7 days later.</li>
            <li>Public league data is not personal data and stays.</li>
          </ul>
        </div>
      </Section>
      <Section title="Contact">
        <div className={styles.prose}>
          <p>Questions or requests: open an issue at <a href={ISSUES} rel="noreferrer">github.com/hhavynn/fantasy/issues</a>.</p>
          <p>See also the <Link href="/terms" prefetch={false}>Terms</Link>.</p>
        </div>
      </Section>
    </div>
  );
}
