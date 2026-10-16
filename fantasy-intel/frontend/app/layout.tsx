import type { Metadata } from "next";
import { Barlow_Condensed, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import Link from "next/link";
import { CommandPalette } from "@/components/CommandPalette";
import { BrandMark, MobileNav, NavGroups, SubNav, TabBar } from "@/components/Nav";
import { ProjectionSwitch } from "@/components/ProjectionSwitch";
import { SyncPill } from "@/components/SyncPill";
import { TableScroll } from "@/components/TableScroll";
import { ThemeSwitch } from "@/components/ThemeSwitch";
import { api } from "@/lib/api";
import { getProjectionSource } from "@/lib/projection-source.server";
import type { StatusPayload } from "@/lib/sync-status";
import { THEME_INIT_SCRIPT } from "@/lib/theme";
import "./globals.css";

export const dynamic = "force-dynamic";

const display = Barlow_Condensed({ subsets: ["latin"], weight: ["500", "600"], variable: "--font-barlow-condensed" });
const sans = IBM_Plex_Sans({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--font-plex-sans" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-plex-mono" });

export const metadata: Metadata = {
  title: { default: "Fantasy Intel", template: "%s · Fantasy Intel" },
  description: "Fantasy football war room for your league",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const [status, projection] = await Promise.all([api<NonNullable<StatusPayload>>("/api/status"), getProjectionSource()]);
  const s = status.data;
  return (
    // suppressHydrationWarning: the inline script below sets data-theme on <html> before React hydrates.
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <header className="appbar">
          <div className="topbar">
            <Link href="/" className="brand" aria-label="Fantasy Intel home">
              <BrandMark />
              <span className="brand-word">Fantasy<span>Intel</span></span>
            </Link>
            <NavGroups />
            <div className="topbar-end">
              {s?.league && (
                <span className="league-chip">
                  {s.league.name} <span className="faint">· wk {s.league.current_week}</span>
                </span>
              )}
              <CommandPalette />
              <ProjectionSwitch value={projection} className="topbar-proj" />
              <ThemeSwitch className="topbar-theme" />
              <SyncPill status={s ?? null} />
              <MobileNav projection={projection} />
            </div>
          </div>
          <SubNav />
        </header>
        <main>{children}</main>
        <TabBar />
        <TableScroll />
      </body>
    </html>
  );
}
