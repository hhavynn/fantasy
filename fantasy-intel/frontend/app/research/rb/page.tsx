import { PositionResearch } from "../PositionResearch";

export const metadata = { title: "RB research" };

export default function Page({ searchParams }: PageProps<"/research/rb">) {
  return <PositionResearch slug="rb" searchParams={searchParams} />;
}
