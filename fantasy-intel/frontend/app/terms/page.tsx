import Link from "@/components/LeagueLink";
import { PageHeader, Section } from "@/components/ui";
import styles from "./legal.module.css";

export const metadata = { title: "Terms" };

const ISSUES = "https://github.com/hhavynn/fantasy/issues";

export default function TermsPage() {
  return (
    <div className="stack">
      <PageHeader eyebrow="About" title="Terms" meta="Last updated: October 20, 2026" />
      <Section title="A free, non-commercial site">
        <div className={styles.prose}>
          <p>This site is free to use. It has no ads and takes no payments.</p>
          <p>It is not affiliated with, endorsed by or connected to ESPN, Sleeper or the NFL.</p>
        </div>
      </Section>
      <Section title="ESPN leagues">
        <div className={styles.prose}>
          <p>ESPN league data is read through ESPN&apos;s undocumented API. ESPN can change or stop it at any time, and
            ESPN leagues may then stop working here.</p>
          <p>A private ESPN league is connected only with your own ESPN cookies. They are stored encrypted (AES-256-GCM,
            with the key wrapped by RSA-OAEP), used only to read that league, and never shown back to you or anyone
            else.</p>
        </div>
      </Section>
      <Section title="Sleeper leagues">
        <div className={styles.prose}>
          <p>Sleeper league data comes from Sleeper&apos;s public, read-only API.</p>
        </div>
      </Section>
      <Section title="No guarantees">
        <div className={styles.prose}>
          <p>Projections on this site are estimates. The site and everything on it are provided as is, with no
            warranty of any kind.</p>
        </div>
      </Section>
      <Section title="Acceptable use">
        <div className={styles.prose}>
          <p>Do not scrape the site or abuse it, for example by sending automated or excessive requests.</p>
          <p>Accounts or leagues that break these terms can be removed.</p>
        </div>
      </Section>
      <Section title="Privacy and contact">
        <div className={styles.prose}>
          <p>What is stored and how to delete it is in the <Link href="/privacy" prefetch={false}>Privacy policy</Link>.</p>
          <p>Questions or problems: open an issue at <a href={ISSUES} rel="noreferrer">github.com/hhavynn/fantasy/issues</a>.</p>
        </div>
      </Section>
    </div>
  );
}
