import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import { Method, Notice, PageHeader, ProvenanceBadge, Section, Verdict, VerdictFigure } from "@/components/ui";
import { api } from "@/lib/api";
import {
  CONTRACT, MAX_CHARS, PATH, askHref, askPath, isCard, isSample, linkHref, noAnswerTitle, readQuestion, sampleText,
  valueText, type AskCard, type AskCell, type AskColumn,
} from "@/lib/ask";
import { Missing } from "@/lib/format";
import { withLeague } from "@/lib/league-scope";
import { currentLeague } from "@/lib/league-scope.server";
import { ALL_NAV_PAGES } from "@/lib/navigation";
import { signalsOf, type ProvenancePayload } from "@/lib/signal-provenance";
import { readTeamSelection, selectedTeamId, withTeamId, type TeamsPayload } from "@/lib/team-perspective";
import styles from "./ask.module.css";

export const metadata = { title: "Ask" };

const TITLE = "Ask";
const EYEBROW = "Players · Research";
const FIGURES = 4;               // facts in the verdict; the rest are listed under it

type SP = Record<string, string | string[] | undefined>;

/** The nav label of the page a card links to ("Open Start/Sit"), or a neutral label. */
function pageLabel(path: string): string {
  return ALL_NAV_PAGES.find((p) => p.href === path)?.label ?? "the full view";
}

function Cell({ cell, column }: { cell: AskCell | undefined; column: AskColumn }) {
  if (cell === undefined) return <Missing />;
  if (isSample(cell)) {
    const text = valueText(cell.value, column.unit === "percent" ? "%" : null);
    if (text === null) return <Missing why="Missing for this player in the window: never counted as 0" />;
    const sample = sampleText(cell);
    return <span title={sample ? `Over ${sample}` : undefined}>{text}</span>;
  }
  const text = valueText(cell, column.unit);
  return text === null ? <Missing /> : <>{text}</>;
}

// Ask Fantasy Intel (issue #219): one question about this league, answered from an existing analysis or declined. The
// backend (GET /api/ask, contract ask/v1) parses the question with a deterministic grammar, runs exactly one existing
// read-only analysis and copies its numbers; this page shows the card it returns and links to the page with the same
// view. The form is a plain GET, so it works without JavaScript.
export default async function AskPage({ searchParams }: PageProps<"/ask">) {
  const sp = (await searchParams) as SP;
  const { q, tooLong } = readQuestion(sp);
  const sel = readTeamSelection(sp);
  if (sel.kind === "invalid") {
    return (
      <div className="stack">
        <PageHeader eyebrow={EYEBROW} title={TITLE} actions={<TeamPerspective path={PATH} sp={sp} current={null} />} />
        <UnknownTeam path={PATH} sel={sel} />
      </div>
    );
  }
  const team = selectedTeamId(sel);
  const action = withLeague(PATH, (await currentLeague())?.id);
  // Without a question the backend's empty answer carries the supported question shapes, so they live in one place.
  // The picker shows the team the answer is for: the URL's, else the default (saved or owner's) team, which a link
  // never writes into the URL.
  const [res, teams] = await Promise.all([
    tooLong ? Promise.resolve(null) : api<AskCard>(withTeamId(askPath(q), sel)),
    api<TeamsPayload>("/api/teams"),
  ]);
  const inView = team ?? (teams.ok ? teams.data.default_team_id ?? null : null);
  const card = res?.ok && isCard(res.data) ? res.data : null;
  const answer = card?.status === "answered" ? card.answer : null;

  return (
    <div className="stack">
      <PageHeader
        eyebrow={EYEBROW}
        title={TITLE}
        meta="Questions this league's data can answer exactly. Anything else is declined, never guessed."
        actions={<TeamPerspective path={PATH} sp={sp} current={inView} reset={[]} />}
        provenance={answer && card?.provenance ? <ProvenanceBadge signals={signalsOf(card.provenance as ProvenancePayload)} /> : undefined}
      />

      <form className={styles.form} action={action} method="get" role="search" aria-label="Ask a question">
        <label className={styles.field}>
          <span className={styles.label}>Question</span>
          <input type="search" name="q" defaultValue={q} maxLength={MAX_CHARS} autoComplete="off"
            placeholder="Who should I start this week?" aria-describedby="ask-hint" />
        </label>
        {team !== null && <input type="hidden" name="team" value={team} />}
        <button type="submit">Ask</button>
        <p id="ask-hint" className={styles.hint}>
          Lineups, waivers, matchups, players, trade values, byes, the playoff race and player screens. At most {MAX_CHARS} characters.
        </p>
      </form>

      {tooLong && (
        <Notice tone="caution" title="That question is too long">A question is at most {MAX_CHARS} characters. Shorten it and ask again.</Notice>
      )}
      {res && !res.ok && (res.status === 422
        ? <Notice tone="caution" title="That question cannot be read">{res.error}</Notice>
        : <BackendProblem error={res.error} kind={res.kind} status={res.status} />)}
      {res?.ok && !card && (
        <Notice tone="caution" title="The backend sent an unexpected format">
          This page reads the {CONTRACT} response. Update the backend and the app together.
        </Notice>
      )}

      {card && answer && (
        <>
          <Verdict
            eyebrow={card.intent?.label}
            headline={answer.headline}
            figures={answer.facts.slice(0, FIGURES).map((f) => {
              const text = valueText(f.value, f.unit);
              return <VerdictFigure key={f.label} label={f.label} value={text ?? <Missing />} />;
            })}
            actions={card.link ? <Link href={linkHref(card.link)} prefetch={false}>Open {pageLabel(card.link.path)}</Link> : undefined}
          >
            {card.interpretation && <p className={styles.read}><span className={styles.label}>Read as</span> {card.interpretation}</p>}
          </Verdict>
          {answer.facts.length > FIGURES && (
            <dl className={styles.facts}>
              {answer.facts.slice(FIGURES).map((f) => {
                const text = valueText(f.value, f.unit);
                return (
                  <div key={f.label} className={styles.fact}>
                    <dt>{f.label}</dt>
                    <dd>{text ?? <Missing />}</dd>
                  </div>
                );
              })}
            </dl>
          )}
          {answer.rows.length > 0 && (
            <Section title="Details" flush
              footnote={answer.truncated && card.link ? <>The first {answer.rows.length}. <Link href={linkHref(card.link)} prefetch={false}>See all on {pageLabel(card.link.path)}</Link>.</> : undefined}>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>{answer.columns.map((c) => <th key={c.key} scope="col">{c.label}</th>)}</tr>
                  </thead>
                  <tbody>
                    {answer.rows.map((r, i) => (
                      <tr key={`${r.player_id ?? "row"}-${i}`}>
                        {answer.columns.map((c) => <td key={c.key}><Cell cell={r.cells[c.key]} column={c} /></td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          )}
          {card.notes.length > 0 && (
            <ul className={styles.notes} aria-label="Limits of this answer">
              {card.notes.map((n) => <li key={n}>{n}</li>)}
            </ul>
          )}
        </>
      )}

      {card?.status === "clarify" && card.clarify && (
        <Notice tone="info" title={card.clarify.question}>
          <p className={styles.reason}>{card.clarify.reason}</p>
          {card.clarify.options.length > 0 && (
            <ul className={styles.options}>
              {card.clarify.options.map((o) => (
                <li key={o.player_id}>
                  <Link href={askHref(o.ask, team)} prefetch={false}>{o.name}</Link>{" "}
                  <span className="faint">{[o.position, o.pro_team, o.fantasy_team_name ?? "not rostered"].filter(Boolean).join(" · ")}</span>
                </li>
              ))}
            </ul>
          )}
        </Notice>
      )}

      {card && (card.status === "abstain" || card.status === "unavailable") && (
        <Notice tone={card.abstain?.code === "empty" ? "info" : "caution"} title={noAnswerTitle(card)}>
          {card.abstain?.reason && card.abstain.code !== "empty" && <p className={styles.reason}>{card.abstain.reason}</p>}
          {card.link && <p className={styles.reason}><Link href={linkHref(card.link)} prefetch={false}>Open {pageLabel(card.link.path)}</Link></p>}
        </Notice>
      )}

      {card && card.examples.length > 0 && (
        <Section title="Questions Ask can answer" subtitle="Each opens its answer here.">
          <ul className={styles.examples}>
            {card.examples.map((e) => <li key={e}><Link href={askHref(e, team)} prefetch={false}>{e}</Link></li>)}
          </ul>
        </Section>
      )}

      <Method title="How Ask works">
        <p>
          Ask reads a question with a fixed grammar: each supported question shape maps to one existing page&apos;s analysis, with its
          positions, weeks, metrics, numbers and players checked against what that page accepts. A question that adds anything the
          grammar does not cover is declined rather than answered without it. No language model or paid service is involved, and the
          same question always gets the same answer.
        </p>
        <p>
          Every number on an answer is that page&apos;s own number for the same league, team and week, with the same data sources and
          freshness; a missing value is shown as missing, never as 0. Names that match more than one player ask which one you mean. The
          playoff answer is the playoff race&apos;s deterministic scenario, never a probability.
        </p>
      </Method>
    </div>
  );
}
