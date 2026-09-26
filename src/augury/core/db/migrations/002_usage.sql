-- Tokens from models without a known price; [budget] daily_tokens caps these (spec §5.9).
ALTER TABLE runs ADD COLUMN unpriced_tokens INTEGER NOT NULL DEFAULT 0;
