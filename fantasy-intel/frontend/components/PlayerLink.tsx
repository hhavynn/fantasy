import Link from "next/link";

export function PlayerLink({ id, name }: { id: number; name: string | null }) {
  return <Link href={`/players?id=${id}`}>{name ?? `ESPN #${id}`}</Link>;
}
