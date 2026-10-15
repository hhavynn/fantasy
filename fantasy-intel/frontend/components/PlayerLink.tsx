import Link from "next/link";

export function PlayerLink({ id, name }: { id: number; name: string | null }) {
  return <Link className="player-link" href={`/players?id=${id}`}>{name ?? `ESPN #${id}`}</Link>;
}
