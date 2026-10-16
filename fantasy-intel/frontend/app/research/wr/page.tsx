import { PositionResearch } from "../PositionResearch";

export const metadata = { title: "WR research" };

export default function Page({ searchParams }: PageProps<"/research/wr">) {
  return <PositionResearch slug="wr" searchParams={searchParams} />;
}
