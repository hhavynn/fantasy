import type { Metadata } from "next";
import { AppHeader } from "@/components/AppHeader";
import { api } from "@/lib/api";
import type { SyncInfo } from "@/lib/types";
import "./globals.css";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Fantasy Intel",
  description: "Local fantasy football war room",
};

type Status = {
  league: { name: string; current_week: number } | null;
  sync: SyncInfo;
  nfl_latest_week: number | null;
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const status = await api<Status>("/api/status");
  const s = status.data;

  return (
    <html lang="en">
      <body>
        <AppHeader status={s} />
        <main>{children}</main>
      </body>
    </html>
  );
}
