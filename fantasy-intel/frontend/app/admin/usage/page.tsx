import { notFound } from "next/navigation";
import { connection } from "next/server";
import { PageHeader, Section } from "@/components/ui";
import { apiRequest } from "@/lib/api";

export const metadata = { title: "Usage" };

type Metric = { metric: string; used: number; quota: number | null; unit: string; sampled_at: string;
  max_used_7d: number | null; percent: number | null };

const fmt = (n: number | null) => (n === null ? "–" : n.toLocaleString("en-US", { maximumFractionDigits: 3 }));

/** Free-tier usage for platform admins (Phase 6), sampled hourly by the platform monitor. Anyone else: not found. */
export default async function UsagePage() {
  await connection();
  const res = await apiRequest<{ metrics: Metric[] }>("/v2/admin/usage");
  if (!res.ok || !res.data) notFound();
  const metrics = res.data.metrics;
  return (
    <div className="stack">
      <PageHeader eyebrow="Admin" title="Free-tier usage"
                  meta="Sampled hourly by the platform monitor. At 70% of a quota it opens a GitHub issue." />
      <Section title="Latest samples">
        {metrics.length === 0 ? <p>No samples yet: the monitor has not run.</p> : (
          <table>
            <thead>
              <tr><th>Metric</th><th>Used</th><th>Quota</th><th>Unit</th><th>%</th><th>7-day max</th><th>Sampled</th></tr>
            </thead>
            <tbody>
              {metrics.map((m) => (
                <tr key={m.metric} style={m.percent !== null && m.percent >= 70 ? { fontWeight: 600, color: "var(--negative)" } : undefined}>
                  <td>{m.metric}</td><td>{fmt(m.used)}</td><td>{fmt(m.quota)}</td><td>{m.unit}</td>
                  <td>{m.percent === null ? "–" : `${m.percent}%`}</td><td>{fmt(m.max_used_7d)}</td>
                  <td>{new Date(m.sampled_at).toISOString().slice(0, 16).replace("T", " ")} UTC</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </div>
  );
}
