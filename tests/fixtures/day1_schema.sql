-- The Day 1 (v0.1, FETCH_VERSION 1) schema, kept to test migrating old databases.
CREATE TABLE IF NOT EXISTS issues (
    number        INTEGER PRIMARY KEY,
    title         VARCHAR NOT NULL,
    state         VARCHAR NOT NULL,      -- OPEN | CLOSED
    state_reason  VARCHAR,               -- COMPLETED | NOT_PLANNED | DUPLICATE | REOPENED
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL,
    closed_at     TIMESTAMP,
    author        VARCHAR,               -- NULL for deleted accounts
    author_type   VARCHAR,               -- User | Bot | Mannequin | ...
    labels        VARCHAR[] NOT NULL,
    assignees     VARCHAR[] NOT NULL,
    url           VARCHAR NOT NULL,
    raw           JSON NOT NULL,         -- GraphQL node as fetched
    ingested_at   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS pull_requests (
    number           INTEGER PRIMARY KEY,
    title            VARCHAR NOT NULL,
    state            VARCHAR NOT NULL,   -- OPEN | CLOSED | MERGED
    is_draft         BOOLEAN,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    merged_at        TIMESTAMP,
    closed_at        TIMESTAMP,
    additions        INTEGER,
    deletions        INTEGER,
    author           VARCHAR,
    author_type      VARCHAR,
    base_ref_name    VARCHAR,
    first_commit_at  TIMESTAMP,          -- author date of the PR's first commit
    first_review_at  TIMESTAMP,          -- first submitted review by someone else
    first_reviewer   VARCHAR,
    labels           VARCHAR[] NOT NULL,
    url              VARCHAR NOT NULL,
    raw              JSON NOT NULL,
    ingested_at      TIMESTAMP NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS ingest_run_id;

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id            INTEGER PRIMARY KEY DEFAULT nextval('ingest_run_id'),
    repo              VARCHAR NOT NULL,
    mode              VARCHAR NOT NULL,  -- full | incremental
    status            VARCHAR NOT NULL,  -- running | success | failed
    started_at        TIMESTAMP NOT NULL,
    finished_at       TIMESTAMP,
    since_requested   TIMESTAMP,         -- --since as given; NULL means all history
    coverage_from     TIMESTAMP,         -- oldest updated_at the database is complete from
    issues_since      TIMESTAMP,         -- lower bound actually sent for issues
    prs_since         TIMESTAMP,         -- lower bound actually used for pull requests
    issues_fetched    INTEGER,
    prs_fetched       INTEGER,
    issues_watermark  TIMESTAMP,         -- newest issue updated_at seen so far
    prs_watermark     TIMESTAMP,         -- newest PR updated_at seen so far
    api_calls         INTEGER,
    api_cost          INTEGER,
    error             VARCHAR
);
