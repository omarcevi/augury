-- One row per discovery agent run (spec §5.6, §7). id is the matching runs row, which holds
-- the spend; session_id names the ADK transcript in sessions.db. Nothing here touches sources:
-- a candidate becomes a source only when the user confirms it (chosen_json).
CREATE TABLE discovery_runs (
  id              TEXT PRIMARY KEY,
  query           TEXT NOT NULL,
  input_kind      TEXT NOT NULL CHECK (input_kind IN ('url', 'name')),
  status          TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed', 'interrupted')),
  tool_calls      INTEGER NOT NULL DEFAULT 0,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  chosen_json     TEXT NOT NULL DEFAULT '[]',
  explanation     TEXT,
  session_id      TEXT,
  tokens_in       INTEGER NOT NULL DEFAULT 0,
  tokens_out      INTEGER NOT NULL DEFAULT 0,
  started_at      TEXT NOT NULL,
  finished_at     TEXT
);
CREATE INDEX discovery_runs_started ON discovery_runs(started_at);
