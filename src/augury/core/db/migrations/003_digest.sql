-- One triage result per item (spec §5.3). relevance is NULL when the item failed triage.
CREATE TABLE triage (
  item_id        TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  run_id         TEXT NOT NULL,
  relevance      INTEGER CHECK (relevance IS NULL OR relevance BETWEEN 0 AND 10),
  why_read       TEXT NOT NULL DEFAULT '',
  tags_json      TEXT NOT NULL DEFAULT '[]',
  flags_json     TEXT NOT NULL DEFAULT '[]',
  model          TEXT NOT NULL,
  prompt_version INTEGER NOT NULL
);

-- The digest for local day `day` (YYYY-MM-DD): items first seen that day, rebuilt by every
-- scout that day (spec §5.4).
CREATE TABLE digests (
  day            TEXT NOT NULL,
  item_id        TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  position       INTEGER NOT NULL,
  final_score    REAL NOT NULL,
  breakdown_json TEXT NOT NULL,
  PRIMARY KEY (day, item_id)
);
CREATE INDEX digests_item ON digests(item_id);

-- TL;DR cache, keyed by what produced it (spec §5.5).
CREATE TABLE summaries (
  item_id        TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  prompt_version INTEGER NOT NULL,
  model          TEXT NOT NULL,
  tldr_json      TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (item_id, prompt_version, model)
);
