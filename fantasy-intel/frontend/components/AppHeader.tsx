import Link from "next/link";
import { MainNavigation } from "@/components/MainNavigation";
import { SyncStatus } from "@/components/SyncStatus";
import type { SyncInfo } from "@/lib/types";

type StatusData = {
  league: { name: string; current_week: number } | null;
  sync: SyncInfo;
  nfl_latest_week: number | null;
} | null;

type AppHeaderProps = {
  status: StatusData;
};

export function AppHeader({ status }: AppHeaderProps) {
  const league = status?.league;

  return (
    <header className="topbar">
      <div className="topbar-identity">
        <Link href="/" className="brand" aria-label="Fantasy Intel homepage">
          fantasy<span>intel</span>
        </Link>
        {league && (
          <span
            className="league-pill"
            title={`${league.name} · week ${league.current_week}`}
          >
            <span className="league-name">{league.name}</span>
            <span className="league-divider" aria-hidden="true">·</span>
            <span className="league-week">wk {league.current_week}</span>
          </span>
        )}
      </div>

      <MainNavigation />

      <div className="topbar-actions">
        <SyncStatus status={status} />
      </div>
    </header>
  );
}
