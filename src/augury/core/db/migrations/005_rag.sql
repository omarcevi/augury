-- M4 retrieval (spec §6.4, §7). chunks is the source of truth and chunks_fts follows it through
-- triggers. The vector index, chunks_vec, is made by code (core/db/chunks_repo.py), not here:
-- its size comes from [embeddings] dimensions, and creating it needs sqlite-vec, which no
-- migration may depend on (without the extension, search is keyword-only: spec N2).
-- AUTOINCREMENT: an id is never reused, so a vector left behind by a deleted item (pruned
-- later, see chunks_repo.prune_vectors) can never collide with, or stand for, a new passage.
CREATE TABLE chunks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id         TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  collection      TEXT NOT NULL CHECK (collection IN ('archive', 'content')),
  source_id       TEXT NOT NULL,
  kind            TEXT NOT NULL,
  trust           TEXT NOT NULL,
  published_day   INTEGER NOT NULL,  -- yyyymmdd, local; first_seen when there's no date
  section         TEXT NOT NULL DEFAULT '',
  page            INTEGER,
  char_start      INTEGER NOT NULL DEFAULT 0,
  char_end        INTEGER NOT NULL DEFAULT 0,
  text            TEXT NOT NULL,
  context_header  TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  embed_model     TEXT,  -- NULL: no vector yet (no embedder, or it was stopped)
  embed_dim       INTEGER,
  chunker_version INTEGER NOT NULL,
  ingested_at     TEXT NOT NULL
);
CREATE INDEX chunks_item ON chunks(item_id, collection);
CREATE INDEX chunks_embed ON chunks(embed_model, embed_dim);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  context_header, text, content='chunks', content_rowid='id',
  tokenize="unicode61 tokenchars '-._'"
);
CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, context_header, text) VALUES (new.id, new.context_header, new.text);
END;
CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, context_header, text)
    VALUES ('delete', old.id, old.context_header, old.text);
END;
CREATE TRIGGER chunks_fts_update AFTER UPDATE OF context_header, text ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, context_header, text)
    VALUES ('delete', old.id, old.context_header, old.text);
  INSERT INTO chunks_fts(rowid, context_header, text) VALUES (new.id, new.context_header, new.text);
END;

-- Cross-source clusters (spec §6.5): items that cover the same thing share a cluster_id (the
-- id of one of them). NULL: in no cluster.
ALTER TABLE items ADD COLUMN cluster_id TEXT;
CREATE INDEX items_cluster ON items(cluster_id);

-- Ask history (spec §5.7). run_id is the `ask` run that holds the spend.
CREATE TABLE asks (
  id             INTEGER PRIMARY KEY,
  run_id         TEXT,
  scope_json     TEXT NOT NULL,
  question       TEXT NOT NULL,
  answer         TEXT NOT NULL,
  citations_json TEXT NOT NULL DEFAULT '[]',
  model          TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX asks_created ON asks(created_at);

-- runs gains kind 'embed': embedding calls made outside a scout or an ask (Enter in search,
-- opening an item, reindex, eval) are spend too (spec §5.9). SQLite can't change a CHECK, so
-- the table is rebuilt with every row and column kept.
CREATE TABLE runs_new (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('scout', 'discovery', 'summarize', 'ask', 'embed')),
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  status          TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed', 'interrupted')),
  error           TEXT,
  stats_json      TEXT NOT NULL DEFAULT '{}',
  tokens_in       INTEGER NOT NULL DEFAULT 0,
  tokens_out      INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0,
  unpriced_tokens INTEGER NOT NULL DEFAULT 0
);
INSERT INTO runs_new (id, kind, started_at, finished_at, status, error, stats_json, tokens_in,
                      tokens_out, cost_usd, unpriced_tokens)
  SELECT id, kind, started_at, finished_at, status, error, stats_json, tokens_in, tokens_out,
         cost_usd, unpriced_tokens FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE INDEX runs_kind_started ON runs(kind, started_at);
