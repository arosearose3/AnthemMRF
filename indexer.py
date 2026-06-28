#!/usr/bin/env python3
"""
indexer.py — Builds the SQLite database from the Anthem MRF index file.

Run once before starting the web app:
    python indexer.py

Streams the 20 GB file without loading it into memory. Writes anthem_index.db.
Expected run time: 20–40 minutes depending on disk speed.

Schema overview:
  meta             — source file metadata (entity name, version, etc.)
  plans            — one row per reporting_plan entry (681K rows)
  network_files    — unique rate files, deduplicated by URL path (no query params)
  rs_network_files — maps reporting_structure index → network file (40M rows)

The rs_idx column is the position of an entry in the reporting_structure array.
Plans and network files are linked through it: find a plan's rs_idx, then look
up all network files where rs_network_files.rs_idx matches.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import ijson
from tqdm import tqdm

ROOT       = Path(__file__).parent
FULL_INDEX = ROOT / "2026-06-01-AnthemData" / "2026-06-01_anthem_index.json"
DB_PATH    = ROOT / "anthem_index.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -131072;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    rs_idx            INTEGER NOT NULL,
    plan_name         TEXT,
    plan_id           TEXT,
    plan_id_type      TEXT,
    plan_market_type  TEXT,
    plan_sponsor_name TEXT,
    issuer_name       TEXT
);

CREATE TABLE IF NOT EXISTS network_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT,
    domain      TEXT,
    url_path    TEXT UNIQUE,
    url         TEXT
);

CREATE TABLE IF NOT EXISTS rs_network_files (
    rs_idx INTEGER NOT NULL,
    nf_id  INTEGER NOT NULL
);
"""

INDICES = """
CREATE INDEX IF NOT EXISTS idx_plans_rs      ON plans(rs_idx);
CREATE INDEX IF NOT EXISTS idx_plans_name    ON plans(plan_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_plans_sponsor ON plans(plan_sponsor_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_plans_id      ON plans(plan_id);
CREATE INDEX IF NOT EXISTS idx_plans_market  ON plans(plan_market_type);
CREATE INDEX IF NOT EXISTS idx_nf_domain     ON network_files(domain);
CREATE INDEX IF NOT EXISTS idx_nf_desc       ON network_files(description COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_rsnf_rs       ON rs_network_files(rs_idx);
"""

COMMIT_EVERY = 500  # rs entries per transaction


def strip_query(url: str) -> str:
    """Return the URL without query params — used as the dedup key for network files."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def read_top_level(path: Path) -> dict:
    with open(path, "rb") as f:
        meta = {}
        for prefix, event, value in ijson.parse(f):
            if prefix == "reporting_structure" and event == "start_array":
                break
            if event in ("string", "number", "boolean") and prefix and "." not in prefix:
                meta[prefix] = value
    return meta


def build() -> None:
    if not FULL_INDEX.exists():
        print(f"[error] Index file not found: {FULL_INDEX}")
        sys.exit(1)

    if DB_PATH.exists():
        print(f"[error] {DB_PATH.name} already exists. Delete it first to rebuild.")
        sys.exit(1)

    size_gb = FULL_INDEX.stat().st_size / 1e9
    print(f"Source : {FULL_INDEX.name}  ({size_gb:.1f} GB)")
    print(f"Output : {DB_PATH}")
    print(f"Expected time: 20–40 minutes\n")

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()

    # ── Pass 1: top-level metadata ────────────────────────────────────────────
    print("Reading metadata...")
    meta = read_top_level(FULL_INDEX)
    for k, v in meta.items():
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))
    con.commit()
    print(f"  Entity : {meta.get('reporting_entity_name')}  v{meta.get('version')}")

    # ── Pass 2: stream reporting_structure ────────────────────────────────────
    print("\nIndexing reporting_structure entries...")

    # In-memory dedup map: url_path → network_file.id
    # Populated lazily as new unique paths are encountered.
    nf_path_to_id: dict[str, int] = {}

    rs_idx    = 0
    plan_buf: list[tuple] = []
    rsnf_buf: list[tuple] = []
    t0        = time.time()

    with open(FULL_INDEX, "rb") as f:
        bar = tqdm(
            ijson.items(f, "reporting_structure.item"),
            desc="  entries",
            unit=" entries",
            dynamic_ncols=True,
        )

        for entry in bar:

            # ── reporting_plans ───────────────────────────────────────────────
            for plan in entry.get("reporting_plans", []):
                plan_buf.append((
                    rs_idx,
                    plan.get("plan_name"),
                    plan.get("plan_id"),
                    plan.get("plan_id_type"),
                    plan.get("plan_market_type"),
                    plan.get("plan_sponsor_name"),
                    plan.get("issuer_name"),
                ))

            # ── in_network_files ──────────────────────────────────────────────
            for inf in entry.get("in_network_files", []):
                loc = inf.get("location", "")
                if not isinstance(loc, str) or not loc.startswith("http"):
                    continue

                path_key = strip_query(loc)

                if path_key not in nf_path_to_id:
                    cur = con.execute(
                        "INSERT OR IGNORE INTO network_files (description, domain, url_path, url) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            inf.get("description"),
                            urlparse(loc).netloc,
                            path_key,
                            loc,
                        ),
                    )
                    if cur.rowcount:
                        nf_id = cur.lastrowid
                    else:
                        nf_id = con.execute(
                            "SELECT id FROM network_files WHERE url_path = ?", (path_key,)
                        ).fetchone()[0]
                    nf_path_to_id[path_key] = nf_id
                else:
                    nf_id = nf_path_to_id[path_key]

                rsnf_buf.append((rs_idx, nf_id))

            rs_idx += 1

            if rs_idx % COMMIT_EVERY == 0:
                con.executemany(
                    "INSERT INTO plans "
                    "(rs_idx,plan_name,plan_id,plan_id_type,plan_market_type,plan_sponsor_name,issuer_name) "
                    "VALUES (?,?,?,?,?,?,?)",
                    plan_buf,
                )
                con.executemany(
                    "INSERT INTO rs_network_files (rs_idx, nf_id) VALUES (?, ?)",
                    rsnf_buf,
                )
                con.commit()
                plan_buf.clear()
                rsnf_buf.clear()
                bar.set_postfix(unique_nf=len(nf_path_to_id), refresh=False)

    # Flush remaining rows
    if plan_buf:
        con.executemany(
            "INSERT INTO plans "
            "(rs_idx,plan_name,plan_id,plan_id_type,plan_market_type,plan_sponsor_name,issuer_name) "
            "VALUES (?,?,?,?,?,?,?)",
            plan_buf,
        )
    if rsnf_buf:
        con.executemany(
            "INSERT INTO rs_network_files (rs_idx, nf_id) VALUES (?, ?)",
            rsnf_buf,
        )

    con.execute("INSERT OR REPLACE INTO meta VALUES ('rs_count', ?)", (str(rs_idx),))
    con.execute(
        "INSERT OR REPLACE INTO meta VALUES ('index_built_at', ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"),),
    )
    con.commit()

    # ── Build indices (after all data is inserted — much faster) ─────────────
    print("\nBuilding indices (2–5 minutes)...")
    con.executescript(INDICES)
    con.commit()

    elapsed = time.time() - t0
    db_size = DB_PATH.stat().st_size / 1e9

    print(f"\nDone in {elapsed / 60:.1f} minutes.")
    print(f"Database: {DB_PATH}  ({db_size:.2f} GB)")
    print(f"  Unique network files : {len(nf_path_to_id):,}")
    print(f"  rs entries           : {rs_idx:,}")
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Anthem MRF SQLite index")
    parser.add_argument("--file", type=Path, default=FULL_INDEX,
                        help=f"Path to the index JSON file (default: {FULL_INDEX})")
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help=f"Output SQLite database path (default: {DB_PATH})")
    parsed = parser.parse_args()
    build(db_path=parsed.db, index_path=parsed.file)
