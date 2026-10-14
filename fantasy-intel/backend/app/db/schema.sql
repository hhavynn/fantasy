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
    opportunity_score    DOUBLE,     -- 0-10 position percentile of recent opportunity (see basis)
    production_score     DOUBLE,     -- 0-10 position percentile of recent PPR/game
    usage_trend_score    DOUBLE,     -- -1..+1 composite of usage deltas
    trend_label          VARCHAR,    -- UP | DOWN | FLAT | INSUFFICIENT
    fp_per_game          DOUBLE,
    xfp_per_game         DOUBLE,
    fp_minus_xfp         DOUBLE,
    value_gap_label      VARCHAR,    -- PRODUCTION_EXCEEDS_USAGE | USAGE_EXCEEDS_PRODUCTION | ALIGNED | INSUFFICIENT
    classification       VARCHAR,    -- = market_signal (kept for compatibility)
    value_score          DOUBLE,     -- 0-10 blended rest-of-season value proxy
    components           JSON,
    reasons              JSON,
    computed_at          TIMESTAMP,
    PRIMARY KEY (season, gsis_id)
);

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
    opportunity_score      DOUBLE,
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
