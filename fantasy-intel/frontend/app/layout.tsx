import type { Metadata } from "next";
import Link from "next/link";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import type { SyncInfo } from "@/lib/types";
import "./globals.css";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Fantasy Intel",
  description: "Local fantasy football war room",
};

type Status = { league: { name: string; current_week: number } | null; sync: SyncInfo; nfl_latest_week: number | null };

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const status = await api<Status>("/api/status");
  const s = status.data;
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <div className="brand">
            fantasy<span>intel</span>
          </div>
          <nav className="nav">
            <Link href="/">Overview</Link>
            <Link href="/players">Players</Link>
            <Link href="/waivers">Waivers</Link>
            <Link href="/trades">Trades</Link>
            <Link href="/trades/discover">Discover trades</Link>
            <Link href="/schedule-intelligence">Schedule</Link>
            <Link href="/playoff-race">Playoff race</Link>
            <Link href="/managers">Managers</Link>
            <Link href="/data">Data quality</Link>
          </nav>
          <div className="syncinfo">
            {s ? (
              <>
                {s.league && (
                  <span>
                    <b>{s.league.name}</b> · week {s.league.current_week}
                  </span>
                )}
                <span>ESPN sync: <b>{when(s.sync.last_success.espn)}</b></span>
                <span>
                  NFL sync: <b>{when(s.sync.last_success.nfl)}</b>
                  {s.nfl_latest_week ? ` (stats thru wk ${s.nfl_latest_week})` : ""}
                </span>
                <span>Analytics: <b>{when(s.sync.last_success.analytics)}</b></span>
              </>
            ) : (
              <span>backend offline</span>
            )}
          </div>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
