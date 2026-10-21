import Link from "next/link";
import { PlayerPhoto } from "./PlayerPhoto";

export function PlayerLink({ id, name }: { id: number; name: string | null }) {
  return (
    <Link className="player-link" href={`/players?id=${id}`}>
      <PlayerPhoto id={id} name={name} inline />{name ?? `Player #${id}`}
    </Link>
  );
}
