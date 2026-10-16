import { PositionResearch } from "../PositionResearch";

export const metadata = { title: "TE research" };

export default function Page({ searchParams }: PageProps<"/research/te">) {
  return <PositionResearch slug="te" searchParams={searchParams} />;
}
