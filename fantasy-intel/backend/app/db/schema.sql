-- fantasy-intel DuckDB schema.
-- Conventions:
--   * Source IDs are preserved verbatim (espn_player_id, team_id, gsis_id, ...).
--   * NULL means "unknown / not available"; 0 means a measured zero.
--   * Every sync is idempotent: rows are replaced by primary key, and
--     per-period snapshots are replaced wholesale for the periods re-synced.
--
-- Raw nflverse tables (nfl_weekly_stats, nfl_snap_counts, nfl_injuries,
-- nfl_schedules, nfl_depth_charts, nfl_rosters_weekly, nfl_ff_opportunity,
-- nfl_ff_playerids) are created by the NFL sync directly from the nflverse
-- column sets so that no source field is lost; they are partitioned by season.

CREATE TABLE IF NOT EXISTS schema_meta (
    key    VARCHAR PRIMARY KEY,
    value  VARCHAR
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id       VARCHAR PRIMARY KEY,
    source       VARCHAR NOT NULL,          -- espn | nfl | analytics
    started_at   TIMESTAMP NOT NULL,
    finished_at  TIMESTAMP,
    status       VARCHAR NOT NULL,          -- running | ok | error
    message      VARCHAR,
    stats        JSON
);

-- ---------------------------------------------------------------- ESPN ----

CREATE TABLE IF NOT EXISTS leagues (
    league_id              BIGINT,
    season                 INTEGER,
    name                   VARCHAR,
    size                   INTEGER,
    scoring_type           VARCHAR,
    reception_points       DOUBLE,           -- 1.0 = full PPR, NULL if not found
    is_faab                BOOLEAN,
    faab_budget            INTEGER,
    current_week           INTEGER,          -- current scoring period
    current_matchup_period INTEGER,
    final_scoring_period   INTEGER,
    reg_season_matchups    INTEGER,
    playoff_teams          INTEGER,
    trade_deadline         TIMESTAMP,
    waiver_process_days    JSON,
    roster_slots           JSON,             -- {"QB":1,"RB":2,...,"BE":7,"IR":1}
    scoring_items          JSON,
    settings_raw           JSON,
    synced_at              TIMESTAMP,
    PRIMARY KEY (league_id, season)
);

CREATE TABLE IF NOT EXISTS league_members (
    league_id     BIGINT,
    season        INTEGER,
    member_id     VARCHAR,
    display_name  VARCHAR,
    first_name    VARCHAR,
    last_name     VARCHAR,
    PRIMARY KEY (league_id, season, member_id)
);

CREATE TABLE IF NOT EXISTS fantasy_teams (
    league_id             BIGINT,
    season                INTEGER,
    team_id               INTEGER,
    abbrev                VARCHAR,
    name                  VARCHAR,
    owner_ids             JSON,
    owner_names           VARCHAR,
    division_id           INTEGER,
    wins                  INTEGER,
    losses                INTEGER,
    ties                  INTEGER,
    points_for            DOUBLE,
    points_against        DOUBLE,
    playoff_seed          INTEGER,
    waiver_rank           INTEGER,
    faab_spent            DOUBLE,
    acquisitions          INTEGER,           -- ESPN transactionCounter
    drops                 INTEGER,
    trades                INTEGER,
    move_to_ir            INTEGER,
    matchup_acquisitions  JSON,              -- {"1": 2, "2": 0, ...} adds per matchup period
    is_mine               BOOLEAN,
    synced_at             TIMESTAMP,
    PRIMARY KEY (league_id, season, team_id)
);

-- ESPN player universe (all active pro players + league availability state).
CREATE TABLE IF NOT EXISTS fantasy_players (
    espn_player_id       BIGINT PRIMARY KEY,
    full_name            VARCHAR,
    position             VARCHAR,            -- QB RB WR TE K D/ST ...
    default_position_id  INTEGER,
    pro_team_id          INTEGER,
    pro_team             VARCHAR,            -- nflverse abbreviation (WAS, LA, ...)
    eligible_slots       JSON,
    injury_status        VARCHAR,            -- ESPN status, NULL if not reported
    injured              BOOLEAN,
    availability_status  VARCHAR,            -- FREEAGENT | WAIVERS | ONTEAM | NULL (unknown)
    on_team_id           INTEGER,
    percent_owned        DOUBLE,
    percent_started      DOUBLE,
    percent_change       DOUBLE,
    season_points        DOUBLE,
    season_avg_points    DOUBLE,
    detail_synced_at     TIMESTAMP,          -- when ownership/injury/availability were last refreshed
    synced_at            TIMESTAMP
);

CREATE TABLE IF NOT EXISTS player_ownership_history (
    snapshot_date    DATE,
    espn_player_id   BIGINT,
    percent_owned    DOUBLE,
    percent_started  DOUBLE,
    percent_change   DOUBLE,
    PRIMARY KEY (snapshot_date, espn_player_id)
);

-- League-scoring fantasy points per player per week (ESPN appliedTotal).
CREATE TABLE IF NOT EXISTS espn_player_weekly_points (
    season           INTEGER,
    espn_player_id   BIGINT,
    scoring_period   INTEGER,
    actual_points    DOUBLE,
    projected_points DOUBLE,
    synced_at        TIMESTAMP,
    PRIMARY KEY (season, espn_player_id, scoring_period)
);
-- projected_points holds ESPN's projection for that week (statSourceId 1), including future
-- weeks current..final fantasy period. It is ESPN's number, not ours. Future projections never
-- touch actual_points; see docs/espn_ros_projections.md for the replacement semantics.
-- actual_source (#254 S3): who supplied actual_points, per row. 'provider' = the provider's own league-scored points
-- (Sleeper players_points), and 'box_score' = Fantasy Intel's nflverse box score scored with the league's skill rules
-- (app/sleeper/write.py, a Sleeper player-week no roster scored; events the box score does not record are not in it).
-- NULL when actual_points is NULL, and on every row of a writer that does not record it: the ESPN sync writes only
-- ESPN's appliedTotal (provider points).
ALTER TABLE espn_player_weekly_points ADD COLUMN IF NOT EXISTS actual_source VARCHAR;

-- One ESPN projection snapshot per day per player-week (latest sync of the day wins), kept
-- for future calibration / backtesting only. Not used by any score or recommendation.
-- A row exists only when ESPN returned a projection entry (0 = projected zero).
CREATE TABLE IF NOT EXISTS espn_projection_history (
    snapshot_date    DATE,
    season           INTEGER,
    espn_player_id   BIGINT,
    scoring_period   INTEGER,
    projected_points DOUBLE,
    synced_at        TIMESTAMP,
    PRIMARY KEY (snapshot_date, season, espn_player_id, scoring_period)
);

-- Rest-of-season ESPN projection summary (derived from espn_player_weekly_points, leagues and
-- nfl_schedules). Totals are NULL unless every expected week has an ESPN projection; a projected
-- 0 counts as available. Byes come from the NFL schedule, never from a 0 projection.
CREATE TABLE IF NOT EXISTS player_ros_projections (
    season                          INTEGER,
    espn_player_id                  BIGINT,
    as_of_week                      INTEGER,     -- league current_week
    final_week                      INTEGER,     -- league final_scoring_period
    last_regular_season_week        INTEGER,     -- NULL if it can't be derived from league settings
    current_week_projection         DOUBLE,
    ros_projected_points            DOUBLE,
    ros_weeks_expected              INTEGER,
    ros_weeks_available             INTEGER,
    ros_coverage                    DOUBLE,
    regular_season_projected_points DOUBLE,
    regular_season_weeks_expected   INTEGER,
    regular_season_weeks_available  INTEGER,
    playoff_projected_points        DOUBLE,
    playoff_weeks_expected          INTEGER,
    playoff_weeks_available         INTEGER,
    bye_weeks_remaining             INTEGER,     -- NULL if the schedule/team is unknown for any remaining week
    bye_weeks                       JSON,
    missing_weeks                   JSON,        -- remaining weeks with no ESPN projection
    projection_source               VARCHAR,     -- 'ESPN'
    projection_synced_at            TIMESTAMP,   -- newest synced_at among the weekly rows used
    computed_at                     TIMESTAMP,
    PRIMARY KEY (season, espn_player_id)
);

-- V2 (schema v6): every team's optimal projected starting lineup for every remaining fantasy week
-- (evaluation start week .. final_scoring_period), from ESPN weekly projections, unmodified, with
-- the league's real slots + exact FLEX rules. A projected 0 is real; a missing projection is
-- unknown and never treated as 0: if a lineup-relevant rostered player with an NFL team lacks a
-- projection, projection_complete = false and projected_lineup_points is NULL (known_lineup_points
-- is the known-only total). Derived; rebuilt by analytics. See docs/v2_trade_evaluator.md.
CREATE TABLE IF NOT EXISTS team_week_lineup_projections (
    league_id                 BIGINT,
    season                    INTEGER,
    team_id                   INTEGER,
    week                      INTEGER,
    phase                     VARCHAR,     -- regular | playoff | NULL (split unknown)
    known_lineup_points       DOUBLE,      -- optimal lineup from known projections only
    projected_lineup_points   DOUBLE,      -- canonical; NULL unless projection_complete
    projection_complete       BOOLEAN,
    assignment                JSON,        -- [{slot, player_id, name, position, points, is_flex}]
    bench                     JSON,        -- [{player_id, name, position, points}] (points NULL = no projection)
    missing_projection_ids    JSON,        -- relevant rostered players with an NFL team and no projection
    teamless_unprojected_ids  JSON,        -- teamless players without a projection (do not poison the week)
    computed_at               TIMESTAMP,
    PRIMARY KEY (league_id, season, team_id, week)
);

CREATE TABLE IF NOT EXISTS team_ros_lineup_projections (
    league_id                                BIGINT,
    season                                   INTEGER,
    team_id                                  INTEGER,
    evaluation_start_week                    INTEGER,
    final_week                               INTEGER,
    last_regular_season_week                 INTEGER,
    ros_projected_starting_points            DOUBLE,    -- NULL unless every week is complete
    ros_known_starting_points                DOUBLE,
    regular_season_projected_starting_points DOUBLE,
    playoff_projected_starting_points        DOUBLE,
    ros_weeks_expected                       INTEGER,
    ros_weeks_complete                       INTEGER,
    missing_weeks                            JSON,
    missing_players                          JSON,
    start_week_reason                        VARCHAR,
    computed_at                              TIMESTAMP,
    PRIMARY KEY (league_id, season, team_id)
);

-- Weekly roster/lineup snapshot per team. The current period is overwritten on
-- each sync; completed periods become the historical record.
CREATE TABLE IF NOT EXISTS lineup_snapshots (
    league_id         BIGINT,
    season            INTEGER,
    scoring_period    INTEGER,
    team_id           INTEGER,
    espn_player_id    BIGINT,
    lineup_slot_id    INTEGER,
    lineup_slot       VARCHAR,
    is_starter        BOOLEAN,
    acquisition_type  VARCHAR,
    acquisition_date  TIMESTAMP,
    injury_status     VARCHAR,
    points            DOUBLE,
    projected_points  DOUBLE,
    synced_at         TIMESTAMP,
    PRIMARY KEY (league_id, season, scoring_period, team_id, espn_player_id)
);

-- One snapshot per day of every team's current-period roster (latest sync of the day wins, except that a
-- snapshot synced before a week's first kickoff is never replaced by a sync after it). Append-only across
-- days, unlike lineup_snapshots, so the rosters at a week's first kickoff survive the sync rewriting that
-- week's lineup_snapshots. Read by power_rankings.load_previous. Create-only (no SCHEMA_VERSION bump).
-- See docs/power_rankings.md.
CREATE TABLE IF NOT EXISTS roster_history (
    snapshot_date     DATE,
    league_id         BIGINT,
    season            INTEGER,
    team_id           INTEGER,
    espn_player_id    BIGINT,
    lineup_slot_id    INTEGER,
    synced_at         TIMESTAMP,
    PRIMARY KEY (snapshot_date, league_id, season, team_id, espn_player_id)
);

CREATE TABLE IF NOT EXISTS matchups (
    league_id        BIGINT,
    season           INTEGER,
    matchup_id       INTEGER,
    matchup_period   INTEGER,
    home_team_id     INTEGER,
    away_team_id     INTEGER,
    home_points      DOUBLE,
    away_points      DOUBLE,
    winner           VARCHAR,
    playoff_tier     VARCHAR,
    PRIMARY KEY (league_id, season, matchup_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    league_id               BIGINT,
    season                  INTEGER,
    transaction_id          VARCHAR PRIMARY KEY,
    type                    VARCHAR,          -- FREEAGENT | WAIVER | TRADE_ACCEPT | ROSTER | ...
    status                  VARCHAR,          -- EXECUTED | CANCELED | FAILED_* | PENDING
    team_id                 INTEGER,
    scoring_period          INTEGER,
    proposed_date           TIMESTAMP,
    process_date            TIMESTAMP,
    bid_amount              DOUBLE,
    execution_type          VARCHAR,
    related_transaction_id  VARCHAR,
    member_id               VARCHAR,
    is_pending              BOOLEAN,
    raw                     JSON,
    synced_at               TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transaction_items (
    transaction_id       VARCHAR,
    item_index           INTEGER,
    item_type            VARCHAR,             -- ADD | DROP | TRADE | LINEUP | DRAFT
    espn_player_id       BIGINT,
    from_team_id         INTEGER,
    to_team_id           INTEGER,
    from_lineup_slot_id  INTEGER,
    to_lineup_slot_id    INTEGER,
    PRIMARY KEY (transaction_id, item_index)
);

-- League activity feed (communication board). Used to recover trade legs that
-- mTransactions2 omits for other teams.
CREATE TABLE IF NOT EXISTS activity_messages (
    league_id        BIGINT,
    season           INTEGER,
    message_id       VARCHAR PRIMARY KEY,
    topic_id         VARCHAR,
    message_type_id  INTEGER,
    action           VARCHAR,                 -- FA ADDED | WAIVER ADDED | DROPPED | TRADED
    topic_date       TIMESTAMP,
    team_id          INTEGER,
    from_team_id     INTEGER,
    to_team_id       INTEGER,
    espn_player_id   BIGINT,
    bid_amount       DOUBLE,
    raw              JSON
);

CREATE TABLE IF NOT EXISTS draft_picks (
    league_id       BIGINT,
    season          INTEGER,
    overall_pick    INTEGER,
    round           INTEGER,
    round_pick      INTEGER,
    team_id         INTEGER,
    espn_player_id  BIGINT,
    bid_amount      DOUBLE,
    keeper          BOOLEAN,
    PRIMARY KEY (league_id, season, overall_pick)
);

-- ----------------------------------------------------------------- NFL ----

CREATE TABLE IF NOT EXISTS nfl_players (
    gsis_id              VARCHAR PRIMARY KEY,
    display_name         VARCHAR,
    first_name           VARCHAR,
    last_name            VARCHAR,
    position             VARCHAR,
    position_group       VARCHAR,
    latest_team          VARCHAR,
    status               VARCHAR,
    espn_id              BIGINT,
    pfr_id               VARCHAR,
    sleeper_id           VARCHAR,
    birth_date           DATE,
    rookie_season        INTEGER,
    years_of_experience  INTEGER,
    draft_year           INTEGER,
    draft_round          INTEGER,
    draft_pick           INTEGER
);

-- Play-by-play derived player usage (see docs/nfl_metrics_verified_contract.md).
--   rush play     = play_type 'run', not a kneel, not a 2-pt try (scrambles INCLUDED)
--   designed rush = rush play with qb_scramble = 0
--   target        = play_type 'pass', pass_attempt = 1, sack = 0, receiver present, not 2-pt
CREATE TABLE IF NOT EXISTS nfl_pbp_player_week (
    season              INTEGER,
    week                INTEGER,
    player_id           VARCHAR,    -- gsis
    team                VARCHAR,
    rush_attempts       INTEGER,    -- rush plays (incl. scrambles, excl. kneels)
    designed_rushes     INTEGER,    -- rush plays excluding scrambles
    scrambles           INTEGER,
    designed_rush_success INTEGER,  -- designed rushes with success = 1 (EPA > 0)
    explosive_runs      INTEGER,    -- designed rushes with yards_gained >= 10
    rz_carries          INTEGER,    -- designed rushes with yardline_100 <= 20
    inside10_carries    INTEGER,
    gl_carries          INTEGER,    -- designed rushes with yardline_100 <= 5
    targets             INTEGER,
    rz_targets          INTEGER,
    inside10_targets    INTEGER,
    ftn_charted_targets INTEGER,    -- targets with any FTN read code; NULL = game not charted
    -- Raw FTN read_thrown code counts. Numeric semantics are UNVERIFIED (nflverse/nflreadr#325);
    -- experimental display only, never used in scores or recommendations.
    ftn_read0_targets   INTEGER,
    ftn_read1_targets   INTEGER,
    ftn_read2_targets   INTEGER,
    designed_targets    INTEGER,    -- FTN 'DES'
    checkdown_targets   INTEGER,    -- FTN 'CHK'
    scramble_drill_targets INTEGER, -- FTN 'SD'
    PRIMARY KEY (season, week, player_id)
);

-- Offensive team-week environment.
CREATE TABLE IF NOT EXISTS nfl_team_week (
    season                  INTEGER,
    week                    INTEGER,
    team                    VARCHAR,
    opponent                VARCHAR,
    team_rush_attempts      INTEGER,   -- rush plays incl. scrambles, excl. kneels/2-pt
    team_designed_rushes    INTEGER,
    team_scrambles          INTEGER,
    team_rb_carries         INTEGER,   -- designed rushes by RBs (backfield denominator)
    team_targets            INTEGER,
    team_rz_carries         INTEGER,   -- designed rushes inside the 20
    team_rz_targets         INTEGER,
    team_ftn_read1_targets  INTEGER,   -- FTN code "1" targets (UNVERIFIED semantics); NULL if not charted
    team_ftn_charted_targets INTEGER,
    dropbacks               INTEGER,   -- qb_dropback = 1 (incl. sacks and scrambles)
    sacks                   INTEGER,
    hit_or_sack             INTEGER,   -- dropbacks with sack = 1 OR qb_hit = 1 (never summed)
    sack_rate               DOUBLE,
    hit_or_sack_rate        DOUBLE,
    pfr_pressures           INTEGER,   -- PFR times_pressured summed over team QBs
    pfr_pressure_rate       DOUBLE,    -- pfr_pressures / PBP dropbacks
    proe                    DOUBLE,    -- mean(pass_oe) over run/pass plays, percentage points
    proe_plays              INTEGER,
    neutral_pass_rate       DOUBLE,    -- mean(pass) with 0.20 <= wp <= 0.80 and > 2 min left in half
    neutral_plays           INTEGER,
    offense_plays           INTEGER,   -- run/pass plays (sanity check vs snaps)
    team_offense_snaps      INTEGER,   -- derived from PFR offense_snaps / offense_pct (validated)
    PRIMARY KEY (season, week, team)
);

-- Defensive team-week counts (aggregate across weeks for matchup context).
CREATE TABLE IF NOT EXISTS nfl_defense_week (
    season               INTEGER,
    week                 INTEGER,
    defteam              VARCHAR,
    opponent             VARCHAR,   -- offense faced
    pass_plays           INTEGER,   -- pass = 1 (dropbacks incl. sacks/scrambles) with EPA
    pass_epa_sum         DOUBLE,
    rush_plays           INTEGER,   -- designed rushes with EPA
    rush_epa_sum         DOUBLE,
    rush_success         INTEGER,
    explosive_runs       INTEGER,
    targets              INTEGER,
    rb_targets           INTEGER,
    wr_targets           INTEGER,
    te_targets           INTEGER,
    PRIMARY KEY (season, week, defteam)
);

-- Betting-implied team totals from nflverse schedules.
-- spread_line > 0 means the HOME team is favored (verified upstream + against results).
CREATE TABLE IF NOT EXISTS team_game_lines (
    season          INTEGER,
    week            INTEGER,
    game_id         VARCHAR,
    team            VARCHAR,
    opponent        VARCHAR,
    is_home         BOOLEAN,
    spread_line     DOUBLE,
    total_line      DOUBLE,
    implied_total   DOUBLE,    -- home: (total + spread)/2, away: (total - spread)/2
    PRIMARY KEY (season, week, team)
);

-- Season-to-date defensive context (through `through_week`) with sample sizes and ranks.
CREATE TABLE IF NOT EXISTS defense_context (
    season                     INTEGER,
    through_week               INTEGER,
    defteam                    VARCHAR,
    games                      INTEGER,
    pass_plays                 INTEGER,
    pass_epa_allowed           DOUBLE,
    rush_plays                 INTEGER,
    rush_epa_allowed           DOUBLE,
    rush_success_allowed       DOUBLE,
    explosive_run_rate_allowed DOUBLE,
    targets                    INTEGER,
    rb_target_share_allowed    DOUBLE,
    te_target_share_allowed    DOUBLE,
    ranks                      JSON,      -- 1 = most generous to offenses
    PRIMARY KEY (season, defteam)
);

-- Season-to-date offensive environment per team.
CREATE TABLE IF NOT EXISTS team_context (
    season               INTEGER,
    through_week         INTEGER,
    team                 VARCHAR,
    games                INTEGER,
    proe                 DOUBLE,
    neutral_pass_rate    DOUBLE,
    sack_rate            DOUBLE,
    hit_or_sack_rate     DOUBLE,
    pfr_pressure_rate    DOUBLE,
    dropbacks            INTEGER,
    next_week            INTEGER,
    next_opponent        VARCHAR,
    next_implied_total   DOUBLE,
    PRIMARY KEY (season, team)
);

-- ------------------------------------------------------------ Mapping ----

CREATE TABLE IF NOT EXISTS player_id_map (
    espn_player_id    BIGINT PRIMARY KEY,
    gsis_id           VARCHAR,          -- NULL when unmatched / team defense
    match_method      VARCHAR,          -- espn_id:nfl_players | espn_id:ff_playerids | espn_id:rosters_weekly | name_position_team | team_defense | unmatched
    match_confidence  VARCHAR,          -- high | medium | n/a | none
    espn_name         VARCHAR,
    espn_position     VARCHAR,
    espn_team         VARCHAR,
    nfl_name          VARCHAR,
    nfl_position      VARCHAR,
    nfl_team          VARCHAR,
    notes             VARCHAR,
    mapped_at         TIMESTAMP
);

-- Write-once log of every injury-feed retrieval (nflverse report / ESPN status), recorded at sync time
-- from our own clock. Append-only: the code only ever INSERTs (identical content fetched again is a new
-- row with the same content_sha, never an overwrite). provider_published_at is the provider's own
-- timestamp (nflverse date_modified) and stays NULL when the provider supplies none; ESPN status has none.
-- Create-only (no SCHEMA_VERSION bump). See docs/injury_freshness.md.
CREATE TABLE IF NOT EXISTS injury_retrievals (
    retrieval_id               VARCHAR PRIMARY KEY,
    source                     VARCHAR NOT NULL,   -- nflverse | espn_status
    scope                      VARCHAR NOT NULL,   -- team_week (nflverse) | team_current (ESPN status) | feed (whole nflverse injury frame)
    season                     INTEGER NOT NULL,
    week                       INTEGER,            -- NULL for ESPN status (a current status) and feed rows
    team                       VARCHAR,            -- NULL for feed rows
    fetch_started_at           TIMESTAMP,          -- naive UTC, before the download began
    retrieved_at               TIMESTAMP NOT NULL, -- naive UTC, when the download returned
    content_sha                VARCHAR NOT NULL,   -- sha256 of this scope's rows as downloaded
    row_count                  INTEGER NOT NULL,
    rows_with_publication_time INTEGER NOT NULL,
    provider_published_at      TIMESTAMP,          -- latest provider date_modified; NULL when none supplied
    sync_run_id                VARCHAR,
    recorded_at                TIMESTAMP NOT NULL
);

-- ---------------------------------------------------------- Analytics ----

CREATE TABLE IF NOT EXISTS player_week_usage (
    season                INTEGER,
    week                  INTEGER,
    gsis_id               VARCHAR,
    team                  VARCHAR,
    opponent              VARCHAR,
    position              VARCHAR,
    offense_snaps         INTEGER,
    offense_pct           DOUBLE,      -- 0..1, NULL if no snap record
    team_offense_snaps    INTEGER,
    targets               INTEGER,
    receptions            INTEGER,
    receiving_yards       DOUBLE,
    receiving_tds         INTEGER,
    air_yards             DOUBLE,
    target_share          DOUBLE,      -- targets / team targets (nflverse definition)
    air_yards_share       DOUBLE,
    wopr                  DOUBLE,      -- 1.5*target_share + 0.7*air_yards_share
    adot                  DOUBLE,      -- air yards / target, NULL if 0 targets
    carries               INTEGER,     -- nflverse official carries (includes kneels)
    rushing_yards         DOUBLE,
    rushing_tds           INTEGER,
    rush_attempts         INTEGER,     -- PBP rush plays incl. scrambles, excl. kneels
    designed_rushes       INTEGER,
    team_rush_attempts    INTEGER,
    team_rush_share       DOUBLE,      -- rush_attempts / team_rush_attempts
    team_rb_carries       INTEGER,
    backfield_rush_share  DOUBLE,      -- RBs only: designed_rushes / team RB designed rushes
    yards_per_carry       DOUBLE,      -- NULL if 0 carries
    rushing_success_rate  DOUBLE,      -- successful designed rushes / designed rushes
    explosive_runs        INTEGER,
    explosive_run_rate    DOUBLE,
    ybc_per_att           DOUBLE,      -- PFR yards before contact / PFR carries
    yac_per_att           DOUBLE,      -- PFR yards after contact / PFR carries
    broken_tackles        INTEGER,     -- PFR rushing + receiving broken tackles
    rz_targets            INTEGER,
    rz_carries            INTEGER,     -- designed rushes inside the 20
    gl_carries            INTEGER,     -- designed rushes inside the 5
    ftn_read1_targets     INTEGER,     -- EXPERIMENTAL: FTN read_thrown code "1" (semantics unverified)
    ftn_read1_target_share DOUBLE,     -- EXPERIMENTAL: player / team code-"1" targets; not used in scoring
    designed_targets      INTEGER,
    checkdown_targets     INTEGER,
    ngs_rush_yoe_per_att  DOUBLE,      -- NGS; NULL = not qualified/unavailable (never 0-filled)
    ngs_avg_separation    DOUBLE,      -- NGS; NULL = not qualified/unavailable
    -- External utilization (LEFT JOIN; NULL = not provided). Never used in scores yet.
    routes                INTEGER,
    route_participation   DOUBLE,      -- provider-defined denominator
    tprr                  DOUBLE,      -- nflverse targets / external routes; NULL if routes is 0 or missing
    yprr                  DOUBLE,      -- nflverse receiving yards / external routes; NULL if routes is 0 or missing
    source_tprr           DOUBLE,      -- provider's own TPRR (comparison only)
    source_yprr           DOUBLE,
    pass_snaps            INTEGER,
    pass_block_snaps      INTEGER,
    pass_block_rate       DOUBLE,
    slot_rate             DOUBLE,
    wide_rate             DOUBLE,
    inline_rate           DOUBLE,
    backfield_rate        DOUBLE,
    two_minute_route_share DOUBLE,     -- provider-defined situation
    long_down_route_share  DOUBLE,     -- provider-defined situation
    short_down_route_share DOUBLE,     -- provider-defined situation
    man_tprr              DOUBLE,
    zone_tprr             DOUBLE,
    route_data_provider   VARCHAR,
    route_data_dataset    VARCHAR,
    route_data_updated_at TIMESTAMP,
    opportunities         INTEGER,     -- targets + carries
    fantasy_points_ppr    DOUBLE,
    league_points         DOUBLE,
    xfp                   DOUBLE,
    stats_source          VARCHAR,
    PRIMARY KEY (season, week, gsis_id)
);

CREATE TABLE IF NOT EXISTS player_scores (
    season               INTEGER,
    gsis_id              VARCHAR,
    espn_player_id       BIGINT,
    position             VARCHAR,
    as_of_week           INTEGER,
    games                INTEGER,
    opportunity_basis    VARCHAR,    -- xfp | targets+carries
    opportunity_tier     VARCHAR,    -- HIGH | MEDIUM | LOW (absolute per-position thresholds)
    role_trend           VARCHAR,    -- UP | FLAT | DOWN | INSUFFICIENT
    market_signal        VARCHAR,    -- BUY | SELL | HOLD | WATCH | MONITOR
    opportunity_score    DOUBLE,     -- legacy 0-10, internal policy scale: position percentile of recent opportunity (see basis)
    production_score     DOUBLE,     -- legacy 0-10, internal policy scale: position percentile of recent PPR/game
    usage_trend_score    DOUBLE,     -- -1..+1 composite of usage deltas
    trend_label          VARCHAR,    -- UP | DOWN | FLAT | INSUFFICIENT
    fp_per_game          DOUBLE,
    xfp_per_game         DOUBLE,
    fp_minus_xfp         DOUBLE,
    value_gap_label      VARCHAR,    -- PRODUCTION_EXCEEDS_USAGE | USAGE_EXCEEDS_PRODUCTION | ALIGNED | INSUFFICIENT
    classification       VARCHAR,    -- = market_signal (kept for compatibility)
    value_score          DOUBLE,     -- legacy 0-10, internal policy scale: blended rest-of-season value proxy
    opportunity_score_100 DOUBLE,    -- 0-100.0 (1 dp) mid-rank percentile, from the unrounded rank (ratings-100.v1)
    production_score_100  DOUBLE,    -- 0-100.0 (1 dp) mid-rank percentile of recent PPR/game
    value_score_100       DOUBLE,    -- 0-100.0 (1 dp) clamp(0.75*opp100 + 0.25*prod100 + 10*trend, 0, 100)
    rating_scale_version  VARCHAR,   -- "ratings-100.v1"
    components           JSON,
    reasons              JSON,
    computed_at          TIMESTAMP,
    PRIMARY KEY (season, gsis_id)
);
-- Non-destructive upgrade of an existing table (CREATE TABLE IF NOT EXISTS above is a no-op there). NULL = not computed.
ALTER TABLE player_scores ADD COLUMN IF NOT EXISTS opportunity_score_100 DOUBLE;
ALTER TABLE player_scores ADD COLUMN IF NOT EXISTS production_score_100 DOUBLE;
ALTER TABLE player_scores ADD COLUMN IF NOT EXISTS value_score_100 DOUBLE;
ALTER TABLE player_scores ADD COLUMN IF NOT EXISTS rating_scale_version VARCHAR;

CREATE TABLE IF NOT EXISTS manager_metrics (
    league_id    BIGINT,
    season       INTEGER,
    team_id      INTEGER,
    metric       VARCHAR,
    value        DOUBLE,
    numerator    DOUBLE,
    denominator  DOUBLE,
    sample_size  INTEGER,
    unit         VARCHAR,
    description  VARCHAR,
    computed_at  TIMESTAMP,
    PRIMARY KEY (league_id, season, team_id, metric)
);

CREATE TABLE IF NOT EXISTS manager_classifications (
    league_id    BIGINT,
    season       INTEGER,
    team_id      INTEGER,
    dimension    VARCHAR,
    label        VARCHAR,     -- HIGH | MEDIUM | LOW | INSUFFICIENT
    metric       VARCHAR,
    value        DOUBLE,
    league_mean  DOUBLE,
    league_std   DOUBLE,
    z_score      DOUBLE,
    sample_size  INTEGER,
    min_sample   INTEGER,
    confidence   VARCHAR,     -- LOW | MEDIUM | HIGH
    rule         VARCHAR,
    computed_at  TIMESTAMP,
    PRIMARY KEY (league_id, season, team_id, dimension)
);

CREATE TABLE IF NOT EXISTS team_position_needs (
    league_id          BIGINT,
    season             INTEGER,
    team_id            INTEGER,
    position           VARCHAR,
    starters_required  DOUBLE,
    rostered           INTEGER,
    healthy            INTEGER,
    unavailable        INTEGER,
    effective_available DOUBLE,     -- sum of availability weights (see analytics/availability.py)
    starter_value      DOUBLE,      -- avg value_score of the projected starters
    need_score         DOUBLE,      -- 0..1
    details            JSON,
    computed_at        TIMESTAMP,
    PRIMARY KEY (league_id, season, team_id, position)
);

CREATE TABLE IF NOT EXISTS waiver_recommendations (
    league_id              BIGINT,
    season                 INTEGER,
    espn_player_id         BIGINT,
    position               VARCHAR,
    availability_status    VARCHAR,
    opportunity_score      DOUBLE,    -- legacy 0-10, internal policy scale
    opportunity_score_100  DOUBLE,    -- 0-100.0 (1 dp) waiver-adjusted opportunity (ratings-100.v1); display only
    roster_fit_score       DOUBLE,
    league_interest        VARCHAR,   -- LOW | MEDIUM | HIGH
    interest_score         DOUBLE,
    waiver_score           DOUBLE,
    recommendation         VARCHAR,   -- CLAIM | WAIT | IGNORE
    suggested_drop_id      BIGINT,
    components             JSON,
    reasons                JSON,
    competitors            JSON,
    computed_at            TIMESTAMP,
    PRIMARY KEY (league_id, season, espn_player_id)
);
ALTER TABLE waiver_recommendations ADD COLUMN IF NOT EXISTS opportunity_score_100 DOUBLE;

-- The same recommendations evaluated from every team's roster (league-wide team perspective). Create-only: no
-- schema version bump; the API falls back to waiver_recommendations for the owner's team when this table is missing.
CREATE TABLE IF NOT EXISTS team_waiver_recommendations (
    league_id              BIGINT,
    season                 INTEGER,
    team_id                INTEGER,
    espn_player_id         BIGINT,
    position               VARCHAR,
    availability_status    VARCHAR,
    opportunity_score      DOUBLE,
    opportunity_score_100  DOUBLE,    -- 0-100.0 (1 dp), as in waiver_recommendations (ratings-100.v1); display only
    roster_fit_score       DOUBLE,
    league_interest        VARCHAR,
    interest_score         DOUBLE,
    waiver_score           DOUBLE,
    recommendation         VARCHAR,
    suggested_drop_id      BIGINT,
    components             JSON,
    reasons                JSON,
    competitors            JSON,
    computed_at            TIMESTAMP,
    PRIMARY KEY (league_id, season, team_id, espn_player_id)
);
ALTER TABLE team_waiver_recommendations ADD COLUMN IF NOT EXISTS opportunity_score_100 DOUBLE;

-- Append-only, as-of-time archive of each team's top waiver recommendations (issue #346, app/analytics/waiver_archive.py):
-- one capture per analytics run whose content changed, carried forward across league builds like every league table.
-- Graded by app/analytics/waiver_feedback.py only from captures taken before the week's waiver cutoff. Create-only.
CREATE TABLE IF NOT EXISTS waiver_recommendation_snapshots (
    league_id              BIGINT,
    season                 INTEGER,
    week                   INTEGER,   -- leagues.current_week when captured: the scoring period the claim is for
    team_id                INTEGER,   -- the roster the recommendation was evaluated from
    captured_at            TIMESTAMP, -- UTC: the analytics run's computed_at (one value per capture)
    espn_player_id         BIGINT,
    position               VARCHAR,
    rank                   INTEGER,   -- by waiver_score within (capture, team), ties by player id; 1 = top
    recommendation         VARCHAR,   -- CLAIM | WAIT | IGNORE
    waiver_score           DOUBLE,
    opportunity_score      DOUBLE,
    roster_fit_score       DOUBLE,
    availability_status    VARCHAR,
    suggested_drop_id      BIGINT,
    components             JSON,
    reasons                JSON,
    snapshot_id            VARCHAR,   -- '<league>:<season>:<week>:<team>:<captured_at iso>'
    projection_source      VARCHAR,   -- the waiver run's projection source (projection_source.normalize)
    projection_version     VARCHAR,   -- the newest projection stamp the run read
    scoring_fingerprint    VARCHAR,   -- sha256[:16] of the league's scoring items
    rule_version           VARCHAR,   -- the archive rule (waiver_archive.RULE_VERSION)
    archive_k              INTEGER,   -- candidates kept per team in this capture
    horizon_weeks          INTEGER,   -- the grading horizon this capture was archived for
    roster_synced_at       TIMESTAMP, -- the newest roster snapshot the evaluation used
    content_sha256         VARCHAR,   -- canonical JSON of this (capture, team)'s rows, without captured_at/snapshot_id
    PRIMARY KEY (league_id, season, week, team_id, captured_at, espn_player_id)
);

CREATE TABLE IF NOT EXISTS player_context (
    season          INTEGER,
    gsis_id         VARCHAR,
    position        VARCHAR,
    team            VARCHAR,
    labels          JSON,      -- {role, role_trend, efficiency, scoring_opportunity, offensive_environment, matchup, data_confidence}
    evidence        JSON,      -- the numbers behind each label
    computed_at     TIMESTAMP,
    PRIMARY KEY (season, gsis_id)
);

CREATE TABLE IF NOT EXISTS validation_issues (
    run_id      VARCHAR,
    severity    VARCHAR,       -- error | warning
    check_name  VARCHAR,
    detail      VARCHAR,
    count       INTEGER,
    examples    JSON,
    created_at  TIMESTAMP
);

-- ------------------------------------------------------- External usage ----
-- Provider-neutral ingestion of authorized/manual utilization files. Provider identity
-- lives here as provenance only. These tables are never dropped by migrations.

CREATE TABLE IF NOT EXISTS external_usage_imports (
    import_id          VARCHAR PRIMARY KEY,
    provider           VARCHAR,
    dataset            VARCHAR,
    source_filename    VARCHAR,       -- basename only
    source_file_hash   VARCHAR,       -- sha256 of the file bytes
    season             INTEGER,       -- NULL if the file spans several seasons
    weeks              JSON,
    imported_at        TIMESTAMP,
    source_updated_at  TIMESTAMP,     -- max source_updated_at in the file
    row_count          INTEGER,
    matched_rows       INTEGER,
    unmatched_rows     INTEGER,
    ambiguous_rows     INTEGER,
    warning_count      INTEGER,
    error_count        INTEGER,
    status             VARCHAR,       -- imported | duplicate_noop | rejected
    duplicate_of       VARCHAR,       -- import_id of the identical earlier file
    message            VARCHAR
);

CREATE TABLE IF NOT EXISTS external_player_week_usage (
    provider               VARCHAR,
    dataset                VARCHAR,
    season                 INTEGER,
    week                   INTEGER,
    source_player_key      VARCHAR,   -- id:<provider id> | gsis:<id> | nm:<hash of name|team|pos>
    provider_player_id     VARCHAR,
    gsis_id                VARCHAR,   -- NULL when unmatched/ambiguous (row is still kept)
    player_name            VARCHAR,
    team                   VARCHAR,
    position               VARCHAR,
    routes                 INTEGER,
    route_participation    DOUBLE,
    source_tprr            DOUBLE,
    source_yprr            DOUBLE,
    pass_snaps             INTEGER,
    pass_block_snaps       INTEGER,
    pass_block_rate        DOUBLE,
    slot_rate              DOUBLE,
    wide_rate              DOUBLE,
    inline_rate            DOUBLE,
    backfield_rate         DOUBLE,
    two_minute_route_share DOUBLE,
    long_down_route_share  DOUBLE,
    short_down_route_share DOUBLE,
    man_tprr               DOUBLE,
    zone_tprr              DOUBLE,
    mapping_method         VARCHAR,   -- gsis_id | external_player_map | name_team_position | unmatched | ambiguous
    mapping_confidence     VARCHAR,   -- high | reviewed | medium | none
    mapping_reason         VARCHAR,   -- why unmatched/ambiguous
    source_updated_at      TIMESTAMP,
    imported_at            TIMESTAMP,
    import_id              VARCHAR,
    PRIMARY KEY (provider, dataset, season, week, source_player_key)
);

-- Previous versions of corrected rows (import history stays inspectable).
CREATE TABLE IF NOT EXISTS external_player_week_usage_revisions (
    provider           VARCHAR,
    dataset            VARCHAR,
    season             INTEGER,
    week               INTEGER,
    source_player_key  VARCHAR,
    import_id          VARCHAR,       -- import that produced the superseded values
    superseded_by      VARCHAR,       -- import that replaced them
    superseded_at      TIMESTAMP,
    previous           JSON
);

-- Reviewed / manual identity mappings for provider players.
CREATE TABLE IF NOT EXISTS external_player_map (
    provider            VARCHAR,
    source_player_key   VARCHAR,
    provider_player_id  VARCHAR,
    gsis_id             VARCHAR,
    match_method        VARCHAR,      -- manual | reviewed
    match_confidence    VARCHAR,
    reviewed_at         TIMESTAMP,
    note                VARCHAR,
    PRIMARY KEY (provider, source_player_key)
);

CREATE TABLE IF NOT EXISTS external_usage_issues (
    import_id           VARCHAR,
    severity            VARCHAR,      -- error | warning
    check_name          VARCHAR,
    detail              VARCHAR,
    season              INTEGER,
    week                INTEGER,
    row_number          INTEGER,
    source_player_key   VARCHAR,
    player_name         VARCHAR,
    provider_player_id  VARCHAR,
    created_at          TIMESTAMP
);

-- ------------------------------------------- Independent projections ----
-- Research output of the independent (non-ESPN) weekly model; see docs/independent_projection_engine.md.
-- Create-only tables written ONLY by scripts/backtest_independent_projections.py project; read only by
-- GET /api/projections/independent. Nothing else (lineups, trades, waivers, scores) reads them.
CREATE TABLE IF NOT EXISTS independent_projection_runs (
    run_id               VARCHAR PRIMARY KEY,
    season               INTEGER,
    week                 INTEGER,
    model_version        VARCHAR,
    scoring_fingerprint  VARCHAR,
    artifact_created_at  VARCHAR,
    data_through_season  INTEGER,     -- latest completed (season, week) used by any feature
    data_through_week    INTEGER,
    nfl_synced_at        TIMESTAMP,   -- latest successful live NFL sync before the run
    model_status         JSON,        -- position -> {status, validated_for, start_sit_ready, reasons}
    evaluation           JSON,        -- held-out aggregates from the artifact
    participation_version    VARCHAR, -- participation model (P(appears)); NULL when not applied
    participation_status     JSON,    -- position -> {participation, expected_points, reasons}
    participation_evaluation JSON,    -- held-out participation aggregates
    created_at           TIMESTAMP,
    -- Provenance and integrity (schema v8). Runs are write-once: a committed run and its rows are never
    -- updated or deleted. NULL on pre-v8 runs until the v8 backfill (production_forecasts.migrate_legacy).
    artifact_id          VARCHAR,     -- sha256 of the artifact FILE bytes the `project` path loaded
    content_sha256       VARCHAR,     -- sha256 of the canonicalised run meta + rows (excludes run_id, created_at)
    row_count            INTEGER,     -- rows stored under this run_id; NULL/0 = forecast rows missing
    migrated_from_legacy BOOLEAN,     -- TRUE: backfilled from independent_player_projections (v7 layout)
    -- Rest-of-season projections: the cycle projects every remaining week with the week-W artifact.
    -- forecast_horizon = target week - projected_from_week (0 = the normal pregame forecast). NULL (legacy) = 0.
    forecast_horizon     INTEGER,
    projected_from_week  INTEGER      -- W: the first unplayed week the cycle ran for (the data cutoff week)
);
-- Non-destructive upgrade of an existing v7 table (CREATE TABLE IF NOT EXISTS above is a no-op there).
-- Participation columns (schema v7) for a v6 table: added in place, because the old v7 rebuild DROPPED these
-- tables and with them every stored run.
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS participation_version VARCHAR;
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS participation_status JSON;
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS participation_evaluation JSON;
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS artifact_id VARCHAR;
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS content_sha256 VARCHAR;
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS row_count INTEGER;
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS migrated_from_legacy BOOLEAN;
-- Rest-of-season runs (create-only; never a connection._REBUILD entry: that list DROPS tables).
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS forecast_horizon INTEGER;
ALTER TABLE independent_projection_runs ADD COLUMN IF NOT EXISTS projected_from_week INTEGER;

CREATE TABLE IF NOT EXISTS independent_player_projections (
    season               INTEGER,
    week                 INTEGER,
    espn_player_id       BIGINT,
    gsis_id              VARCHAR,
    run_id               VARCHAR,
    position             VARCHAR,
    nfl_team             VARCHAR,
    opponent             VARCHAR,
    is_home              BOOLEAN,
    projected_points     DOUBLE,      -- NULL = no independent estimate (see coverage_status)
    interval_low         DOUBLE,      -- empirical 80% prediction interval; NULL unless validated + calibrated
    interval_high        DOUBLE,
    model                VARCHAR,     -- context | opportunity
    coverage_status      VARCHAR,     -- projected | abstain_no_history | bye | withheld_unavailable | ...
    games_used           INTEGER,
    latest_usage_season  INTEGER,
    latest_usage_week    INTEGER,
    missing_inputs       JSON,
    flags                JSON,
    explanation          JSON,        -- [{feature, value, training_mean, contribution, text}]
    p_play               DOUBLE,      -- P(appears in week W); NULL = no participation estimate
    expected_points      DOUBLE,      -- p_play * projected_points (unconditional); NULL if either is NULL
    participation        JSON,        -- {p_play, status, depth_rank, explanation, flags, ...}
    PRIMARY KEY (season, week, espn_player_id)
);

ALTER TABLE independent_player_projections ADD COLUMN IF NOT EXISTS p_play DOUBLE;
ALTER TABLE independent_player_projections ADD COLUMN IF NOT EXISTS expected_points DOUBLE;
ALTER TABLE independent_player_projections ADD COLUMN IF NOT EXISTS participation JSON;

-- LEGACY (schema v7): one row per (season, week, player), replaced by the next run of the same week, which is
-- how an earlier run's forecasts were lost. Kept read-only for old readers and as the source of the v8 backfill;
-- never written, dropped or modified by v8. New code writes independent_projection_rows instead.

-- Immutable production forecasts (schema v8). Append-only and run-keyed: every `project` run keeps its own rows.
-- NEVER add this table to a connection._REBUILD entry (that list DROPS tables). The writer
-- (independent_projection_store.write_run) never deletes or updates a committed run; kickoff/pregame are
-- annotations derived at write time from nfl_schedules (naive UTC) and are not part of the content hash.
CREATE TABLE IF NOT EXISTS independent_projection_rows (
    run_id               VARCHAR NOT NULL,
    season               INTEGER,
    week                 INTEGER,
    espn_player_id       BIGINT NOT NULL,
    gsis_id              VARCHAR,
    position             VARCHAR,
    nfl_team             VARCHAR,
    opponent             VARCHAR,
    is_home              BOOLEAN,
    projected_points     DOUBLE,
    interval_low         DOUBLE,
    interval_high        DOUBLE,
    model                VARCHAR,
    coverage_status      VARCHAR,
    games_used           INTEGER,
    latest_usage_season  INTEGER,
    latest_usage_week    INTEGER,
    missing_inputs       JSON,
    flags                JSON,
    explanation          JSON,
    p_play               DOUBLE,
    expected_points      DOUBLE,
    participation        JSON,
    kickoff              TIMESTAMP,   -- the player's own team kickoff (naive UTC); NULL = unknown
    pregame              BOOLEAN,     -- run created_at < kickoff; unknown kickoff = FALSE
    PRIMARY KEY (run_id, espn_player_id)
);

-- Forecast inputs (#225, create-only, no version bump): the full point-model inputs of a projected row and the model
-- they went through, so a forecast change can be split exactly between inputs and model across a retrain
-- (production_forecasts "forecast inputs", projection_attribution). Write-once and sealed by their own digests;
-- never part of a run's content_sha256. Runs stored before these tables existed have none (never backfilled).
-- NEVER list them in a connection._REBUILD entry (that list DROPS tables).
CREATE TABLE IF NOT EXISTS independent_projection_models (
    artifact_id          VARCHAR NOT NULL,   -- == independent_projection_runs.artifact_id of the runs it made
    position             VARCHAR NOT NULL,
    model_kind           VARCHAR NOT NULL,   -- main | fallback (ifp-2.2.0 QB without a start candidate)
    model_version        VARCHAR,
    columns              VARCHAR[],          -- design columns ("<f>__missing" = null indicator of f)
    means                DOUBLE[],           -- training means (imputation + centering)
    stds                 DOUBLE[],
    coef                 DOUBLE[],           -- per standardized column
    intercept            DOUBLE,
    model_sha256         VARCHAR,
    PRIMARY KEY (artifact_id, position, model_kind)
);

CREATE TABLE IF NOT EXISTS independent_projection_row_inputs (
    run_id               VARCHAR NOT NULL,
    espn_player_id       BIGINT NOT NULL,
    artifact_id          VARCHAR,
    position             VARCHAR,
    model_kind           VARCHAR,
    base_run_id          VARCHAR,            -- NULL: the whole row is here; else the cycle run whose row this differs from
    changed              INTEGER[],          -- with base_run_id: the column positions that differ from it
    input_values         DOUBLE[],           -- raw design row aligned with the model's columns (imputed = training mean),
                                             -- or the values at `changed`
    imputed              BOOLEAN[],          -- TRUE: the input was unknown and filled with the training mean
    mixture_column       VARCHAR,            -- QB start/no-start mixture: the start-share column (its value = the share)
    scale                DOUBLE,             -- league re-score factor applied to the reference point (rescore.py)
    shift                DOUBLE,             -- league re-score shift instead of a factor (rescore.py)
    inputs_version       VARCHAR,
    inputs_digest        UBIGINT,            -- first 64 bits of a sha256 over the row as stored
    PRIMARY KEY (run_id, espn_player_id)
);

-- Approved weekly artifacts (create-only, no version bump). ifp-2.0.0 retrains every week, so its approval is
-- one row per artifact, written ON CONFLICT DO NOTHING when the artifact (saved through the write-once weekly
-- store) is used by `project`. A run is served / counted as evidence only if its artifact_id (the sha256 of the
-- artifact FILE bytes) is registered here. NEVER update or delete a row, and never list this table in a
-- connection._REBUILD entry (that list DROPS tables). The hosted app reads this table, never the registry file.
CREATE TABLE IF NOT EXISTS approved_artifacts (
    model_version        VARCHAR NOT NULL,
    artifact_sha256      VARCHAR NOT NULL,   -- == independent_projection_runs.artifact_id of the runs it made
    target_season        INTEGER,            -- the week the artifact was trained for; NULL = any week
    target_week          INTEGER,
    training_cutoff_key  INTEGER,            -- artifact provenance data_cutoff_key (season * 100 + week)
    artifact_created_at  VARCHAR,
    source               VARCHAR NOT NULL,   -- learning_cycle | manual_pin
    registered_at        TIMESTAMP,
    PRIMARY KEY (model_version, artifact_sha256)
);

-- Published projection-performance reports (create-only, no version bump). Built OFFLINE by
-- scripts/projection_performance.py from the research-only ESPN comparison and the recorded Start/Sit decision sets
-- (polars/numpy), so the hosted API can serve the scorecard with SQL alone (app/analytics/performance_report.py).
-- Write-once: one row per distinct report, keyed by the sha256 of its canonical JSON text, inserted in one statement
-- and never updated or deleted; readers recompute the hash and ignore a row that does not verify. Aggregates only:
-- no player, roster, league or team identifiers. Never list this table in a connection._REBUILD entry.
CREATE TABLE IF NOT EXISTS projection_performance_reports (
    report_sha256        VARCHAR PRIMARY KEY,   -- sha256 of `payload` (UTF-8 bytes, exactly as stored)
    season               INTEGER NOT NULL,
    schema_version       VARCHAR NOT NULL,      -- performance_report.SCHEMA_VERSION
    generated_at         TIMESTAMP NOT NULL,    -- naive UTC, also inside the payload
    payload              VARCHAR NOT NULL       -- canonical JSON (sorted keys, no NaN)
);

-- Weekly model replays (create-only, no version bump): the production model refitted walk-forward on every fully
-- final week of the season, built OFFLINE by scripts/model_replay.py and served by app/analytics/model_replay.py.
-- Hindsight, never prospective evidence. Write-once rows keyed by the sha256 of the canonical JSON; a new row only
-- when the model, input (data) or output fingerprint changed. Aggregates only. Never list this table in a connection._REBUILD entry.
CREATE TABLE IF NOT EXISTS model_replays (
    replay_sha256        VARCHAR PRIMARY KEY,   -- sha256 of `payload` (UTF-8 bytes, exactly as stored)
    season               INTEGER NOT NULL,
    schema_version       VARCHAR NOT NULL,      -- model_replay.SCHEMA_VERSION
    generated_at         TIMESTAMP NOT NULL,    -- naive UTC, also inside the payload
    model_fingerprint    VARCHAR NOT NULL,      -- version, config and the source of every module the replay loaded
    data_fingerprint     VARCHAR NOT NULL,      -- the replay's inputs (store rows and features, QB candidates)
    model_version        VARCHAR NOT NULL,      -- index columns for the history list (also inside the payload)
    weeks                VARCHAR NOT NULL,      -- JSON list of the replayed weeks
    mae_all              DOUBLE,                -- season MAE, all positions
    n_all                INTEGER NOT NULL,
    payload              VARCHAR NOT NULL       -- canonical JSON (sorted keys, no NaN)
);

-- Kicker projection inputs derived from nflverse play-by-play (app/nfl/kicking_pbp.py). Create-only (no version
-- bump); rebuilt per season by the live sync, the history sync and scripts/sync_kicking_pbp.py.
-- temp / wind / roof are nflverse's OBSERVED game conditions: never a pregame forecast.
CREATE TABLE IF NOT EXISTS nfl_kick_attempts (
    season INTEGER, week INTEGER, game_id VARCHAR, play_id DOUBLE, posteam VARCHAR, defteam VARCHAR,
    home_team VARCHAR, away_team VARCHAR, kicker_player_id VARCHAR, kicker_player_name VARCHAR,
    kick_type VARCHAR,               -- FG | PAT
    kick_distance DOUBLE,
    result VARCHAR,                  -- made | missed | blocked (| aborted for a PAT snap that was never kicked)
    qtr DOUBLE, half_seconds_remaining DOUBLE, score_differential DOUBLE,
    roof VARCHAR, surface VARCHAR, temp DOUBLE, wind DOUBLE, stadium_id VARCHAR, location VARCHAR,
    PRIMARY KEY (game_id, play_id)
);

-- One row per (game, offense): drive outcomes and kicking opportunities (nflverse fixed_drive segmentation).
CREATE TABLE IF NOT EXISTS nfl_team_game_drives (
    season INTEGER, week INTEGER, game_id VARCHAR, team VARCHAR, opponent VARCHAR, is_home BOOLEAN,
    location VARCHAR, roof VARCHAR, surface VARCHAR, temp DOUBLE, wind DOUBLE, stadium_id VARCHAR,
    drives INTEGER, td_drives INTEGER, fg_drives INTEGER, punt_drives INTEGER, turnover_drives INTEGER,
    downs_drives INTEGER, end_half_drives INTEGER,
    fg_range_drives INTEGER,         -- drives with a scrimmage snap at or inside the opponent 35
    rz_drives INTEGER, rz_td_drives INTEGER, rz_fg_drives INTEGER, avg_start_yardline DOUBLE,
    fg_att INTEGER, fg_made INTEGER, fg_att_50 INTEGER, pat_att INTEGER, pat_made INTEGER, two_pt_att INTEGER,
    fourth_go_in_range INTEGER, fourth_in_range INTEGER, plays INTEGER, epa_per_play DOUBLE, success_rate DOUBLE,
    turnovers INTEGER, points DOUBLE,
    kicker_id VARCHAR,               -- most FG/PAT tries this game
    kickoff_kicker_id VARCHAR,       -- most kickoffs this game
    game_kicker_id VARCHAR,          -- kicker_id, else kickoff_kicker_id (a kicker who dressed but never tried a kick)
    PRIMARY KEY (game_id, team)
);

-- Write-once weather forecast snapshots (app/analytics/kicker_weather.py). One row per retrieval; a later
-- retrieval for the same game is a new row, never an update. NWS (api.weather.gov) only: US public domain data.
CREATE TABLE IF NOT EXISTS weather_forecasts (
    forecast_id      VARCHAR PRIMARY KEY,   -- sha256 of (provider, game, venue, kickoff, retrieved_at, nonce)
    provider         VARCHAR,               -- nws
    provider_model   VARCHAR,               -- e.g. nws:forecastHourly + gridpoints
    game_id          VARCHAR,
    venue_id         VARCHAR,
    latitude         DOUBLE,
    longitude        DOUBLE,
    kickoff_utc      TIMESTAMP,
    window_start_utc TIMESTAMP,
    window_end_utc   TIMESTAMP,
    retrieved_at     TIMESTAMP,             -- our clock, naive UTC
    issued_at        TIMESTAMP,             -- provider updateTime, naive UTC (NULL if not given)
    status           VARCHAR,               -- ok | unavailable | error
    error            VARCHAR,
    forecast_values  JSON,                  -- window aggregates (see kicker_weather.SUMMARY_FIELDS)
    units            JSON
);

-- Kicker projection runs (kfp-*), parallel to independent_projection_runs/rows and deliberately NOT stored there:
-- the skill-position tables are read by several evidence readers that select runs by season only, and a kicker
-- run is made under a different (kicker) scoring fingerprint. Same contract: append-only, write-once, sealed by
-- content_sha256 over the canonical meta + rows (app/analytics/kicker_store.py), verified by every reader.
-- Never list these tables in a connection._REBUILD entry.
CREATE TABLE IF NOT EXISTS kicker_projection_runs (
    run_id               VARCHAR PRIMARY KEY,
    season               INTEGER,
    week                 INTEGER,
    model_version        VARCHAR,
    scoring_fingerprint  VARCHAR,           -- kicker_scoring fingerprint ('k:...')
    scoring              JSON,              -- resolved kicker rules + reconciliation summary
    artifact_id          VARCHAR,           -- sha256 of the artifact file bytes
    artifact_created_at  VARCHAR,
    data_through_season  INTEGER,
    data_through_week    INTEGER,
    nfl_synced_at        TIMESTAMP,
    model_status         JSON,
    evaluation           JSON,
    weather_summary      JSON,
    forecast_horizon     INTEGER,
    projected_from_week  INTEGER,
    created_at           TIMESTAMP,
    content_sha256       VARCHAR,
    row_count            INTEGER
);

CREATE TABLE IF NOT EXISTS kicker_projection_rows (
    run_id               VARCHAR,
    season               INTEGER,
    week                 INTEGER,
    espn_player_id       BIGINT,
    gsis_id              VARCHAR,
    nfl_team             VARCHAR,
    opponent             VARCHAR,
    is_home              BOOLEAN,
    venue_id             VARCHAR,
    coverage_status      VARCHAR,           -- projected | bye | withheld_unavailable | not_expected_kicker | ...
    projected_points     DOUBLE,            -- conditional on kicking in the game
    interval_low         DOUBLE,
    interval_high        DOUBLE,
    p_play               DOUBLE,
    expected_points      DOUBLE,            -- p_play * projected_points
    stat_line            JSON,              -- expected FG tries / makes by distance, PAT tries / makes
    context              JSON,              -- model inputs actually used (team, opponent, venue, weather)
    explanation          JSON,
    flags                JSON,
    missing_inputs       JSON,
    kickoff              TIMESTAMP,         -- annotations (not hashed)
    pregame              BOOLEAN,
    PRIMARY KEY (run_id, espn_player_id)
);

-- D/ST projection runs (dfp-*), parallel to the kicker tables and for the same reasons (a D/ST run is made under a
-- different, D/ST scoring fingerprint). Same contract: append-only, write-once, sealed by content_sha256 over the
-- canonical meta + rows (app/analytics/dst_store.py), verified by every reader. Never list these tables in a
-- connection._REBUILD entry.
CREATE TABLE IF NOT EXISTS dst_projection_runs (
    run_id               VARCHAR PRIMARY KEY,
    season               INTEGER,
    week                 INTEGER,
    model_version        VARCHAR,
    scoring_fingerprint  VARCHAR,           -- D/ST scoring fingerprint ('dst:...')
    scoring              JSON,              -- resolved D/ST rules
    artifact_id          VARCHAR,           -- sha256 of the artifact file bytes
    artifact_created_at  VARCHAR,
    data_through_season  INTEGER,
    data_through_week    INTEGER,
    nfl_synced_at        TIMESTAMP,
    model_status         JSON,
    evaluation           JSON,
    inputs               JSON,              -- the lines as stored at projection time + ESPN statistics cache provenance
    forecast_horizon     INTEGER,
    created_at           TIMESTAMP,
    content_sha256       VARCHAR,
    row_count            INTEGER
);

CREATE TABLE IF NOT EXISTS dst_projection_rows (
    run_id               VARCHAR,
    season               INTEGER,
    week                 INTEGER,
    espn_player_id       BIGINT,
    nfl_team             VARCHAR,
    opponent             VARCHAR,
    is_home              BOOLEAN,
    coverage_status      VARCHAR,           -- projected | bye | no_nfl_team
    projected_points     DOUBLE,
    interval_low         DOUBLE,
    interval_high        DOUBLE,
    expected_points      DOUBLE,            -- = projected_points (a D/ST always plays its game)
    stat_line            JSON,              -- expected sacks, takeaways, points / yards allowed, P(any D/ST TD)
    context              JSON,              -- the line used (spread, total, opponent implied total), kickoff
    explanation          JSON,
    flags                JSON,
    missing_inputs       JSON,
    kickoff              TIMESTAMP,         -- annotations (not hashed)
    pregame              BOOLEAN,
    PRIMARY KEY (run_id, espn_player_id)
);
