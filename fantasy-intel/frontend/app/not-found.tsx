import type { Metadata } from "next";
import { PageHeader, Section } from "@/components/ui";
import NotFoundLinks from "./NotFoundLinks";

export const metadata: Metadata = { title: "Page not found" };

export default function NotFound() {
  return (
    <div className="stack">
      <PageHeader title="Page not found" meta="That address doesn't match a page in Fantasy Intel." />
      <Section title="Go to">
        <NotFoundLinks />
      </Section>
    </div>
  );
}
