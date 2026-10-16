
const TONE: Record<string, string> = {
  BUY: "good", CLAIM: "good", UP: "good", HIGH: "info", ALIGNED: "",
  SELL: "bad", DOWN: "bad", IGNORE: "", LOW: "",
  HOLD: "", FLAT: "", MEDIUM: "warn", WAIT: "warn", MONITOR: "warn",
  INSUFFICIENT: "", USAGE_EXCEEDS_PRODUCTION: "good", PRODUCTION_EXCEEDS_USAGE: "bad",
  BELOW_EXPECTATION: "info", ABOVE_EXPECTATION: "warn",
  OUT: "bad", INJURY_RESERVE: "bad", SUSPENSION: "bad", DOUBTFUL: "bad", QUESTIONABLE: "warn",
  Out: "bad", Doubtful: "bad", Questionable: "warn",
};

const SHORT: Record<string, string> = {
  USAGE_EXCEEDS_PRODUCTION: "Usage > production",
  PRODUCTION_EXCEEDS_USAGE: "Production > usage",
  INSUFFICIENT: "Insufficient data",
  ABOVE_EXPECTATION: "Production above xFP",
  BELOW_EXPECTATION: "Production below xFP",
};

/** Availability designations render as fantasy-standard letter codes; the full word goes in the tooltip. */
const STATUS: Record<string, { code: string; full: string }> = {
  QUESTIONABLE: { code: "Q", full: "Questionable" }, Questionable: { code: "Q", full: "Questionable" },
  DOUBTFUL: { code: "D", full: "Doubtful" }, Doubtful: { code: "D", full: "Doubtful" },
  OUT: { code: "O", full: "Out" }, Out: { code: "O", full: "Out" },
  INJURY_RESERVE: { code: "IR", full: "Injured reserve" }, IR: { code: "IR", full: "Injured reserve" },
  SUSPENSION: { code: "SSPD", full: "Suspended" },
};

/** Acronyms that stay upper case when other ALL_CAPS enum values are sentence-cased. */
const KEEP_CASE = new Set(["BYE", "FA", "FAAB", "PPR", "NFL", "ESPN"]);

function display(value: string): string {
  if (SHORT[value]) return SHORT[value];
  if (KEEP_CASE.has(value) || !/^[A-Z]{2,}(_[A-Z]+)*$/.test(value)) return value;
  const words = value.toLowerCase().replace(/_/g, " ");
  return words[0].toUpperCase() + words.slice(1);
}

export function Badge({ value, title, tone }: { value?: string | null; title?: string; tone?: string }) {
  if (!value) return <span className="missing" title={title ?? "Not available"}>—</span>;
  const t = tone ?? TONE[value] ?? "";
  const status = STATUS[value];
  if (status) {
    return (
      <span className={`badge status ${tone ?? TONE[value] ?? "bad"}`} title={title ? `${status.full} · ${title}` : status.full}>
        {status.code}
      </span>
    );
  }
  // Explicit tones mark caller-supplied text (team names, custom labels), which keeps its own casing.
  return (
    <span className={`badge ${t}`} title={title}>
      {tone === undefined ? display(value) : SHORT[value] ?? value}
    </span>
  );
}
