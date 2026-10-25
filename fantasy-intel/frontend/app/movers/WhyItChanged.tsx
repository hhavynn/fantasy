import { Missing } from "@/lib/format";
import { plural } from "@/lib/plural";
import {
  attributionHeadline, attributionRows, fullInputsText, inputValue, kindText, reconcileText, sideText, signedText,
  unattributedInputs,
  weeksText, type Attribution, type AttributionInput, type AttributionPart,
} from "@/lib/projection-attribution";
import type { Mover } from "@/lib/projection-movers";
import styles from "./movers.module.css";

function Value({ v, unit, why }: { v: number | null | undefined; unit: AttributionPart["unit"]; why: string | null | undefined }) {
  const text = inputValue(v, unit);
  return text === null ? <Missing why={why ?? "No value stored"} /> : <>{text}</>;
}

function BeforeAfter({ p }: { p: Pick<AttributionPart, "before" | "after" | "unit" | "before_missing" | "after_missing" | "varies"> }) {
  if (p.varies) return <p className={styles.whyLine}>Value differs by week (it depends on the opponent or the week).</p>;
  return (
    <p className={styles.whyLine}>
      Earlier <Value v={p.before} unit={p.unit} why={p.before_missing} /> → latest <Value v={p.after} unit={p.unit} why={p.after_missing} />
      {p.before_missing && <span className={styles.whyMeta}> · earlier: {p.before_missing}</span>}
      {p.after_missing && <span className={styles.whyMeta}> · latest: {p.after_missing}</span>}
    </p>
  );
}

function partDetail(p: AttributionPart, a: Attribution) {
  switch (p.kind) {
    case "availability":
      return "The model's chance that he plays changed. This part is that change times the earlier points if he plays.";
    case "input":
      if (p.features) return `Inputs whose shares are each under 0.05 points: ${(p.feature_labels ?? p.features).join("; ")}.`;
      if (!p.exact) return a.approximation_note ?? "Approximate share.";
      return a.full_inputs
        ? "This input's change times the average of the two models' weights for it, times the latest chance he plays."
        : "This input's stored term changed by this much, times the latest chance he plays.";
    case "model":
      if (p.key === "model:coefficients") {
        return "The model was retrained between the two forecasts. This is the change its new weights and intercept "
          + "make by themselves, at the average of the two forecasts' inputs, times the latest chance he plays.";
      }
      if (p.key === "model:rescoring") {
        return "This league's re-scoring of the model's points (a factor or shift from his recent box scores) differs "
          + "between the two forecasts; this is that change by itself.";
      }
      return p.key === "model:availability"
        ? "The availability model or method differs between the two forecasts, so this change is not assigned to anything."
        : "The points model differs between the two forecasts, so the change in points if he plays is not split between inputs.";
    case "status":
      return "One forecast projected him and the other did not. The reason shown is the one stored with the forecast.";
    default:
      return null;
  }
}

function PartRow({ p, a }: { p: AttributionPart; a: Attribution }) {
  const showValues = (p.kind === "input" && !p.features) || p.kind === "availability";
  return (
    <li className={styles.whyRow}>
      <details>
        <summary className={styles.whySummary}>
          <span className={styles.whyLabel}>{p.label}</span>
          <span className={styles.whyKind}>{kindText(p.kind, p.exact, p.measured)}</span>
          <span className={styles.whyPts}>{signedText(p.points)}</span>
        </summary>
        <div className={styles.whyBody}>
          {showValues && <BeforeAfter p={{ ...p, unit: p.kind === "availability" ? "share" : p.unit }} />}
          <p className={styles.whyMeta}>{weeksText(p.weeks)}</p>
          <p className={styles.whyLine}>{partDetail(p, a)}</p>
          {(p.events ?? []).length > 0 && (
            <ul className={styles.whyList}>
              {(p.events ?? []).map((e, i) => <li key={i}>{e.forecast === "earlier" ? "Earlier" : "Latest"} forecast: {e.text} ({e.source})</li>)}
            </ul>
          )}
          {p.kind === "model" && !p.measured && a.model.reasons.length > 0 && (
            <ul className={styles.whyList}>
              {a.model.reasons.map((r, i) => <li key={i}>{r.text} ({weeksText(r.weeks)})</li>)}
            </ul>
          )}
        </div>
      </details>
    </li>
  );
}

function ResidualRow({ a }: { a: Attribution }) {
  const r = a.residual;
  if (!r) return null;
  return (
    <li className={styles.whyRow}>
      <details>
        <summary className={styles.whySummary}>
          <span className={styles.whyLabel}>Residual</span>
          <span className={styles.whyKind}>Not explained</span>
          <span className={styles.whyPts}>{signedText(r.points)}</span>
        </summary>
        <div className={styles.whyBody}>
          {r.reasons.length === 0 ? (
            <p className={styles.whyLine}>Nothing is left over.</p>
          ) : (
            <ul className={styles.whyList}>
              {r.reasons.map((x) => <li key={x.key}>{x.label}: {signedText(x.points)} ({weeksText(x.weeks)})</li>)}
            </ul>
          )}
        </div>
      </details>
    </li>
  );
}

function InputsList({ inputs }: { inputs: AttributionInput[] }) {
  if (inputs.length === 0) return null;
  return (
    <details className={styles.whyMore}>
      <summary>Inputs stored without a measured share ({inputs.length})</summary>
      <ul className={styles.whyList}>
        {inputs.map((i) => (
          <li key={i.feature}>
            <strong>{i.label}</strong>: {i.varies ? "differs by week" : (
              <>
                <Value v={i.before} unit={i.unit} why={i.before_missing} /> → <Value v={i.after} unit={i.unit} why={i.after_missing} />
              </>
            )} <span className={styles.whyMeta}>({weeksText(i.weeks)})</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function MissingInputs({ a }: { a: Attribution }) {
  const rows = [
    ...a.missing_inputs.before.map((m) => ({ side: "Earlier", ...m })),
    ...a.missing_inputs.after.map((m) => ({ side: "Latest", ...m })),
  ];
  if (rows.length === 0) return null;
  return (
    <div className={styles.sigGroup}>
      <h4 className={styles.groupTitle}>Missing inputs</h4>
      <ul className={styles.whyList}>
        {rows.map((m, i) => <li key={i}>{m.side} forecast: {m.text} ({weeksText(m.weeks)})</li>)}
      </ul>
    </div>
  );
}

/** "Why it changed": the mover's change split into measured parts and a residual that add up to it (contract
 * projection-attribution/v1). Every number is the backend's; nothing here infers a cause. */
export function WhyItChanged({ m }: { m: Mover }) {
  const a = m.attribution;
  if (a === undefined) return null;
  return (
    <div className={styles.why} id="why">
      <h3 className={styles.panelTitle}>Why it changed</h3>
      {a === null ? (
        <p className={styles.muted}>The split is read for the listed risers and fallers and the selected player only.</p>
      ) : a.status === "unavailable" ? (
        <p className={styles.muted}>{a.reason}</p>
      ) : (
        <>
          <p className={styles.headline}>{attributionHeadline(a)}</p>
          <ul className={styles.whyRows} aria-label={`Parts of the change for ${m.name}`}>
            {attributionRows(a).map((p) => <PartRow key={p.key} p={p} a={a} />)}
            <ResidualRow a={a} />
          </ul>
          <p className={styles.sigMeta}>{reconcileText(a)}</p>
          {fullInputsText(a) && <p className={styles.sigMeta}>{fullInputsText(a)}</p>}
          <InputsList inputs={unattributedInputs(a)} />
          <MissingInputs a={a} />
          {a.as_of && (
            <p className={styles.sigMeta}>
              Earlier forecast {sideText(a.as_of.before)}. Latest forecast {sideText(a.as_of.after)}.
              {a.as_of.refused.length > 0 && <> {a.as_of.refused.length} {plural(a.as_of.refused.length, "week")} failed the as-of check and stay in the residual.</>}
            </p>
          )}
          {a.method_note && <p className={styles.note}>{a.method_note}</p>}
          {a.approximation_note && <p className={styles.note}>{a.approximation_note}</p>}
          <p className={styles.note}>Source: {a.source_label}. Parts are measured from the two stored forecasts; they are not shown to be football causes.</p>
        </>
      )}
    </div>
  );
}
