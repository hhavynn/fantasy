-- Tenancy database: users, the league registry, who is connected to which league, and the last good
-- provider snapshots. A SEPARATE DuckDB file (FANTASY_TENANCY_DB_PATH), never the analytics file:
-- app/db/publish.py copies only the analytics database to MotherDuck, so none of this is ever published.
-- No foreign keys: DuckDB rejects updates of rows referenced by a key, and every query joins explicitly.
-- No secret material is stored anywhere in this file's tables (credential_refs is metadata only).

CREATE TABLE IF NOT EXISTS app_users (
    user_id       VARCHAR PRIMARY KEY,          -- the verified `sub` of a user assertion
    created_at    TIMESTAMP NOT NULL
);

-- One row per provider league season. league_id is opaque (new_internal_league_id), never derived
-- from the external id. The external id is a string: ESPN 12345 and Sleeper "12345" are different rows.
CREATE TABLE IF NOT EXISTS league_registry (
    league_id           VARCHAR PRIMARY KEY,
    provider            VARCHAR NOT NULL,
    external_league_id  VARCHAR NOT NULL,
    season              INTEGER NOT NULL,
    name                VARCHAR,
    is_public           BOOLEAN,                -- provider-reported visibility; NULL = unknown (treated as private)
    data_origin         VARCHAR NOT NULL CHECK (data_origin IN ('live', 'fixture')),
    created_at          TIMESTAMP NOT NULL,
    UNIQUE (provider, external_league_id, season)
);

-- The authorization table: a principal may act on a league only through an active row here.
CREATE TABLE IF NOT EXISTS league_connections (
    user_id              VARCHAR NOT NULL,
    league_id            VARCHAR NOT NULL,
    role                 VARCHAR NOT NULL,
    access_method        VARCHAR NOT NULL,
    credential_ref       VARCHAR,               -- handle into the vault, never a secret
    my_team_external_id  VARCHAR,
    created_at           TIMESTAMP NOT NULL,
    revoked_at           TIMESTAMP,
    PRIMARY KEY (user_id, league_id)
);

CREATE TABLE IF NOT EXISTS league_sync_status (
    league_id         VARCHAR PRIMARY KEY,
    status            VARCHAR NOT NULL,
    last_synced_at    TIMESTAMP,
    source_updated_at TIMESTAMP,
    last_error        VARCHAR                   -- sanitized (class name + first line, secrets redacted)
);

-- Last good provider payloads, normalized by app/leagues/views.py. API reads serve these, never a live call.
CREATE TABLE IF NOT EXISTS league_snapshots (
    league_id         VARCHAR NOT NULL,
    kind              VARCHAR NOT NULL CHECK (kind IN ('settings', 'teams', 'rosters')),
    payload           JSON NOT NULL,
    provider          VARCHAR NOT NULL,
    source            VARCHAR NOT NULL,
    fetched_at        TIMESTAMP NOT NULL,
    source_updated_at TIMESTAMP,
    PRIMARY KEY (league_id, kind)
);

-- Metadata about a credential held by a vault. The secret itself never touches this database.
CREATE TABLE IF NOT EXISTS credential_refs (
    ref_id     VARCHAR PRIMARY KEY,
    user_id    VARCHAR NOT NULL,
    league_id  VARCHAR NOT NULL,
    provider   VARCHAR NOT NULL,
    kind       VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);
