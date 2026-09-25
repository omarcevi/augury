CREATE TABLE sources (
  id                   TEXT PRIMARY KEY,
  name                 TEXT NOT NULL,
  homepage             TEXT NOT NULL DEFAULT '',
  origin               TEXT NOT NULL CHECK (origin IN ('builtin', 'user')),
  recipe_type          TEXT NOT NULL,
  recipe_json          TEXT NOT NULL,
  trust                TEXT NOT NULL DEFAULT 'publisher' CHECK (trust IN ('publisher', 'community')),
  enabled              INTEGER NOT NULL DEFAULT 1,
  added_via            TEXT NOT NULL DEFAULT 'manual',
  discovery_run_id     TEXT,
  fetch_state_json     TEXT NOT NULL DEFAULT '{}',
  last_success_at      TEXT,
  last_error           TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  created_at           TEXT NOT NULL
);

-- pk is the stable rowid that items_fts points at; id is the public identity.
CREATE TABLE items (
  pk            INTEGER PRIMARY KEY,
  id            TEXT NOT NULL UNIQUE,
  source_id     TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN ('paper', 'article')),
  title         TEXT NOT NULL,
  url           TEXT NOT NULL,
  canonical_url TEXT NOT NULL UNIQUE,
  authors_json  TEXT NOT NULL DEFAULT '[]',
  published_at  TEXT,
  summary       TEXT NOT NULL DEFAULT '',
  arxiv_id      TEXT,
  image_url     TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  times_seen    INTEGER NOT NULL DEFAULT 1,
  content_hash  TEXT NOT NULL,
  is_old        INTEGER NOT NULL DEFAULT 0,
  enriched_at   TEXT
);
CREATE INDEX items_first_seen ON items(first_seen);
CREATE INDEX items_source ON items(source_id);
CREATE INDEX items_arxiv ON items(arxiv_id);

CREATE VIRTUAL TABLE items_fts USING fts5(
  title, summary, content='items', content_rowid='pk', tokenize="unicode61 tokenchars '-._'"
);
CREATE TRIGGER items_fts_insert AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, summary) VALUES (new.pk, new.title, new.summary);
END;
CREATE TRIGGER items_fts_delete AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary) VALUES ('delete', old.pk, old.title, old.summary);
END;
CREATE TRIGGER items_fts_update AFTER UPDATE OF title, summary ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary) VALUES ('delete', old.pk, old.title, old.summary);
  INSERT INTO items_fts(rowid, title, summary) VALUES (new.pk, new.title, new.summary);
END;

CREATE TABLE signals (
  item_id      TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  observed_on  TEXT NOT NULL,
  rank         INTEGER,
  upvotes      INTEGER,
  upvotes7d    INTEGER,
  github_stars INTEGER,
  comments     INTEGER,
  PRIMARY KEY (item_id, observed_on)
);

CREATE TABLE contents (
  item_id           TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  body_md           TEXT NOT NULL DEFAULT '',
  extractor         TEXT NOT NULL,
  extractor_version INTEGER NOT NULL,
  word_count        INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'paywalled')),
  error             TEXT,
  fetched_at        TEXT NOT NULL
);

CREATE TABLE item_state (
  item_id       TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  read_at       TEXT,
  read_progress REAL NOT NULL DEFAULT 0,
  liked         INTEGER NOT NULL DEFAULT 0,
  saved         INTEGER NOT NULL DEFAULT 0,
  hidden        INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL
);

-- Append-only history; no foreign key so it outlives deleted items.
CREATE TABLE interactions (
  id      INTEGER PRIMARY KEY,
  item_id TEXT NOT NULL,
  action  TEXT NOT NULL CHECK (action IN
            ('open', 'like', 'unlike', 'save', 'unsave', 'hide', 'unhide', 'ask')),
  at      TEXT NOT NULL
);
CREATE INDEX interactions_item ON interactions(item_id);

CREATE TABLE runs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('scout', 'discovery', 'summarize', 'ask')),
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed', 'interrupted')),
  error       TEXT,
  stats_json  TEXT NOT NULL DEFAULT '{}',
  tokens_in   INTEGER NOT NULL DEFAULT 0,
  tokens_out  INTEGER NOT NULL DEFAULT 0,
  cost_usd    REAL NOT NULL DEFAULT 0
);
CREATE INDEX runs_kind_started ON runs(kind, started_at);
