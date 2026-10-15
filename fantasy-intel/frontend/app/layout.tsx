import type { Metadata } from "next";
import { Barlow_Condensed, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { Nav } from "@/components/Nav";
import { api } from "@/lib/api";
import { when } from "@/lib/format";
import type { SyncInfo } from "@/lib/types";
import "./globals.css";

export const dynamic = "force-dynamic";

const display = Barlow_Condensed({ subsets: ["latin"], weight: ["500", "600"], variable: "--font-barlow-condensed" });
const sans = IBM_Plex_Sans({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--font-plex-sans" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-plex-mono" });

export const metadata: Metadata = {
  title: "Fantasy Intel",
  description: "Local fantasy football war room",
};

type Status = { league: { name: string; current_week: number } | null; sync: SyncInfo; nfl_latest_week: number | null };

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const status = await api<Status>("/api/status");
  const s = status.data;
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <body>
        <header className="topbar">
          <div className="brand">
            fantasy<span>intel</span>
          </div>
          <Nav />
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
