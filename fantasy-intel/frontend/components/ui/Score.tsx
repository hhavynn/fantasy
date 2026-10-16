/**
 * 0–max value as the number plus ten ticks (countable at a glance, comparable across rows). `unit` adds a
 * visible suffix such as "/100" and `label` names the value for assistive tech ("Opportunity rating: 87.4 out of 100").
 * Player ratings go through `Rating`, which supplies both; the default (max 10, no unit) is for other 0–10 scores.
 */
export function Score({ value, max = 10, unit, label }: { value?: number | null; max?: number; unit?: string; label?: string }) {
  if (value === null || value === undefined) return <span className="missing" title="Not computed">—</span>;
  const on = Math.round(Math.max(0, Math.min(1, value / max)) * 10);
  const text = `${value.toFixed(1)} ${label || unit ? "out of" : "of"} ${max}`;
  return (
    <span className="score">
      <span className="v">{value.toFixed(1)}{unit && <span className="unit">{unit}</span>}</span>
      <span className="ticks" role="img" aria-label={label ? `${label}: ${text}` : text}>
        {Array.from({ length: 10 }, (_, i) => <i key={i} className={i < on ? "on" : undefined} />)}
      </span>
    </span>
  );
}
