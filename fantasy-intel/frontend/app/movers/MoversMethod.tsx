import { Method } from "@/components/ui";
import { providerName, type Provider } from "@/lib/labels";
import { DEFAULT_THRESHOLDS, type MoversResponse } from "@/lib/projection-movers";

/**
 * The rules in plain words. Thresholds come from the response, so the text never drifts from the backend. A league
 * whose provider publishes no projections (Sleeper) has no ESPN snapshots, so their paragraph is left out.
 */
export function MoversMethod({ d, provider = "espn", providerProjections = true }: {
  d: MoversResponse; provider?: Provider; providerProjections?: boolean;
}) {
  const t = d.thresholds ?? DEFAULT_THRESHOLDS;
  return (
    <Method title="How movement is calculated">
      <p>
        <b>What is compared.</b> Two stored forecasts of the same remaining weeks.{providerProjections && <> ESPN&apos;s projections are saved once a day, so each
        day is one snapshot.</>} Fantasy Intel&apos;s own forecasts are compared run by run, and only runs made before the week&apos;s first
        kickoff, under the league&apos;s current scoring, are used. Missing history is never reconstructed, and with fewer than two
        stored forecasts there is nothing to compare.
      </p>
      <p>
        <b>Which weeks.</b> Only weeks that neither forecast could have seen played: each week must be in both forecasts, and its first
        kickoff must come after the latest forecast was taken. Weeks left out are listed above with the reason. Next week compares only
        the first of those weeks. For Fantasy Intel, next week compares the earlier cycle&apos;s forecast of that week, made a week ahead,
        with the latest forecast of the same week.
      </p>
      <p>
        <b>Totals and change.</b> Earlier and latest are the sums of projected points over the compared weeks. Change is latest minus
        earlier. The percent is the change over the earlier total and is blank when that total is under {t.pct_floor} point. A change
        within {t.flat} points either way counts as flat.
      </p>
      <p>
        <b>Rank.</b> Players are ranked within their position by total, highest first, separately in each forecast. Rank move is the
        earlier rank minus the latest rank, so a positive number is a move up.
      </p>
      <p>
        <b>Who is listed.</b> A player needs a projection in every compared week in both forecasts. A bye week counts as 0 only where
        the NFL schedule shows a bye; any other missing week leaves the player out and is counted, never filled with 0. The risers and
        fallers lists keep players averaging at least {t.relevant_per_week} points a week in either forecast and moving by at least {t.min_delta} points.
        The table lists every compared player, up to the backend&apos;s cap.
      </p>
      <p>
        <b>Evidence.</b> For the biggest risers and fallers, the page shows what the data recorded between the two forecasts: usage,
        availability, role and schedule, with the weeks and source behind each value. Only games that kicked off between the two forecasts and were over
        by the latest one (at least three and a half hours before it) are read, and nothing from the compared weeks. {providerName(provider)} keeps no
        history of its injury status, so that status is shown as it stood at the latest check, never as a change. These are observations made over the same period as the change. They are not shown to be its cause, and a
        player with no recorded change shows none.
      </p>
      <p>
        <b>Fantasy Intel&apos;s own forecasts.</b> The change splits exactly into an availability part (the chance the player appears) and a
        per-game part (the points expected when the player plays); the two add up to the change.
      </p>
      <p>
        <b>Not shown.</b> No confidence scores, grades or probabilities, and no advice to add, drop, start, sit or trade anyone.
      </p>
    </Method>
  );
}
