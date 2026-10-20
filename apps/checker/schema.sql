-- Yaqzan checker (ADR-032): what the external checker remembers in Cloudflare D1.
-- Addresses are never stored: each is known by a short hash of its URL.

-- Each address's last confirmed state; written only when it changes.
CREATE TABLE IF NOT EXISTS state (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  up INTEGER NOT NULL CHECK (up IN (0, 1)),
  since INTEGER NOT NULL
);

-- Hourly counts per address for the availability SLO (kept 90 days).
CREATE TABLE IF NOT EXISTS hourly (
  id TEXT NOT NULL,
  project TEXT NOT NULL,
  hour INTEGER NOT NULL,
  total INTEGER NOT NULL CHECK (total >= 1),
  good INTEGER NOT NULL CHECK (good >= 0 AND good <= total),
  PRIMARY KEY (id, hour)
);
