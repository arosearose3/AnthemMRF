"""
colorado.py — Download, parse, and explore Anthem Colorado rate files.

Pipeline (concurrent):
  - DOWNLOAD_WORKERS threads pull files from the CDN in parallel (I/O bound)
  - PARSE_WORKERS processes decompress + parse in parallel (CPU bound, bypasses GIL)
  - Downloads chain into parse jobs via futures; coordinator handles all SQLite writes

Decompression strategy (best available wins):
  - isal  — Intel ISA-L SIMD gzip, 3-5x faster than stdlib gzip (CPU SIMD)
  - rapidgzip — parallel multi-threaded gzip for large files (multiple CPU cores per file)
  - stdlib gzip — fallback if neither is installed

Parsing strategy (best available wins):
  - orjson — Rust/SIMD JSON parser, 3-10x faster than stdlib json; used for small files
  - ijson  — streaming parser; used for large files (memory-safe)

GPU note:
  cupy is available and can reach the RTX 5080, but NVIDIA nvcomp gzip decompression
  requires conda (no pip wheel). isal + rapidgzip cover the decompression speedup via
  CPU SIMD/parallelism instead.
"""

import gzip
import os
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import ijson

try:
    import orjson as _orjson
    _ORJSON = True
except ImportError:
    _orjson = None  # type: ignore
    _ORJSON = False

try:
    import isal.igzip as _isal
    _ISAL = True
except ImportError:
    _isal = None  # type: ignore
    _ISAL = False

try:
    import rapidgzip as _rapidgzip
    _RAPIDGZIP = True
except ImportError:
    _rapidgzip = None  # type: ignore
    _RAPIDGZIP = False

ROOT      = Path(__file__).parent
DATA_DIR  = ROOT / "data" / "co"
CO_DOMAIN = "anthembcbsco.mrf.bcbs.com"

DOWNLOAD_WORKERS    = 4
PARSE_WORKERS       = max(2, (os.cpu_count() or 4) - 1)
ORJSON_MAX_MB       = 200   # compressed MB — above this, fall back to ijson streaming
DOWNLOAD_RETRIES    = 3
DOWNLOAD_RETRY_WAIT = 5     # seconds between retries

_SCHEMA = """
CREATE TABLE IF NOT EXISTS co_files (
    nf_id         INTEGER PRIMARY KEY,
    status        TEXT    NOT NULL DEFAULT 'queued',
    local_path    TEXT,
    file_bytes    INTEGER DEFAULT 0,
    pr_count      INTEGER DEFAULT 0,
    queued_at     TEXT    DEFAULT (datetime('now')),
    downloaded_at TEXT,
    parsed_at     TEXT,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS co_provider_refs (
    nf_id             INTEGER NOT NULL,
    provider_group_id TEXT    NOT NULL,
    npi               TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_copr_npi   ON co_provider_refs(npi);
CREATE INDEX IF NOT EXISTS idx_copr_group ON co_provider_refs(provider_group_id, nf_id);
"""

_lock:   threading.Lock        = threading.Lock()
_worker: threading.Thread | None = None


# ── Setup ─────────────────────────────────────────────────────────────────────

def init_tables(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    con.commit()
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Status ────────────────────────────────────────────────────────────────────

def file_counts(con: sqlite3.Connection) -> dict:
    total = con.execute(
        "SELECT COUNT(*) FROM network_files WHERE domain = ?", (CO_DOMAIN,)
    ).fetchone()[0]

    by_status = {r[0]: r[1] for r in con.execute(
        "SELECT status, COUNT(*) FROM co_files GROUP BY status"
    ).fetchall()}

    done         = by_status.get("done", 0)
    n_dl         = by_status.get("downloading", 0)
    n_parse      = by_status.get("parsing", 0)
    active       = n_dl + n_parse
    errors       = by_status.get("error", 0)
    queued       = by_status.get("queued", 0)

    active_phases = {"downloading": n_dl, "parsing": n_parse} if active else None

    recent_rows = con.execute(
        """
        SELECT nf.description, nf.url_path, cf.pr_count, cf.file_bytes
        FROM   co_files cf
        JOIN   network_files nf ON nf.id = cf.nf_id
        WHERE  cf.status = 'done'
        ORDER  BY cf.nf_id DESC
        LIMIT  6
        """
    ).fetchall()
    recent = [{
        "description": r[0] or "",
        "filename":    (r[1] or "").split("/")[-1],
        "pr_count":    r[2] or 0,
        "file_bytes":  r[3] or 0,
    } for r in recent_rows]

    return {
        "total":         total,
        "queued":        queued,
        "active":        active,
        "done":          done,
        "errors":        errors,
        "worker_alive":  _worker is not None and _worker.is_alive(),
        "active_phases": active_phases,
        "recent":        recent,
        "workers":       {"download": DOWNLOAD_WORKERS, "parse": PARSE_WORKERS},
        "backends":      {"orjson": _ORJSON, "isal": _ISAL, "rapidgzip": _RAPIDGZIP},
    }


def recent_errors(con: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = con.execute(
        """
        SELECT cf.nf_id, nf.description, nf.url_path, cf.error
        FROM   co_files cf
        JOIN   network_files nf ON nf.id = cf.nf_id
        WHERE  cf.status = 'error'
        ORDER  BY cf.nf_id DESC
        LIMIT  ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Queue ─────────────────────────────────────────────────────────────────────

def queue_all(con: sqlite3.Connection) -> int:
    ids = con.execute(
        "SELECT id FROM network_files WHERE domain = ?", (CO_DOMAIN,)
    ).fetchall()
    con.executemany(
        "INSERT OR IGNORE INTO co_files (nf_id) VALUES (?)",
        [(r[0],) for r in ids],
    )
    con.commit()
    return len(ids)


# ── Parse helpers — module-level so ProcessPoolExecutor can pickle them ───────

def _dest(url_path: str) -> Path:
    return DATA_DIR / url_path.rstrip("/").split("/")[-1]


def _decompress_to_bytes(gz_path: Path) -> bytes:
    """Decompress a gzip file to bytes using the fastest available method.
    isal (Intel ISA-L SIMD) is 3-5x faster than stdlib gzip for this path."""
    if _ISAL:
        with _isal.open(gz_path, "rb") as f:
            return f.read()
    with gzip.open(gz_path, "rb") as f:
        return f.read()


def _open_gz(gz_path: Path):
    """Return a readable file-like object for streaming.
    rapidgzip uses multiple threads to decompress a single file in parallel."""
    if _RAPIDGZIP:
        return _rapidgzip.open(gz_path, parallelization=0)  # 0 = auto thread count
    if _ISAL:
        return _isal.open(gz_path, "rb")
    return gzip.open(gz_path, "rb")


def _parse_refs_orjson(raw: bytes) -> list[tuple[str, str]]:
    doc  = _orjson.loads(raw)
    refs: list[tuple[str, str]] = []
    for ref in (doc.get("provider_references") or []):
        gid = str(ref.get("provider_group_id", ""))
        for group in ref.get("provider_groups") or []:
            for npi in group.get("npi") or []:
                refs.append((gid, str(npi)))
    return refs


def _parse_refs_ijson(gz_path: Path) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    with _open_gz(gz_path) as f:
        for ref in ijson.items(f, "provider_references.item"):
            gid = str(ref.get("provider_group_id", ""))
            for group in ref.get("provider_groups", []):
                for npi in group.get("npi", []):
                    refs.append((gid, str(npi)))
    return refs


def _parse_file(gz_path_str: str) -> list[tuple[str, str]]:
    """
    Parse entry point for worker processes. Strategy:
      Small file (≤ ORJSON_MAX_MB compressed): isal decompress → orjson parse
      Large file: rapidgzip/isal streaming → ijson parse
    Both paths use faster-than-stdlib decompression when available.
    """
    gz_path   = Path(gz_path_str)
    file_size = gz_path.stat().st_size
    use_fast  = _ORJSON and file_size < ORJSON_MAX_MB * 1024 * 1024

    if use_fast:
        try:
            raw = _decompress_to_bytes(gz_path)
            return _parse_refs_orjson(raw)
        except MemoryError:
            pass  # too large for in-memory; fall through to streaming
        except Exception:
            pass  # decompress/parse failure; fall through

    return _parse_refs_ijson(gz_path)


def _download_one(nf_id: int, url: str, url_path: str, db_path_str: str) -> tuple[int, str, int]:
    """
    Download one file. Opens its own brief SQLite connection to mark status='downloading'.
    Retries up to DOWNLOAD_RETRIES times with backoff on connection errors.
    """
    dest = _dest(url_path)

    # Mark as downloading (brief own connection — WAL allows concurrent writes)
    _db_status(db_path_str, nf_id, "downloading")

    if dest.exists():
        return nf_id, str(dest), dest.stat().st_size

    req   = urllib.request.Request(url, headers={"User-Agent": "AnthemMRFExplorer/1.0"})
    last_exc: Exception = RuntimeError("no attempts made")

    for attempt in range(DOWNLOAD_RETRIES):
        try:
            total = 0
            with urllib.request.urlopen(req, timeout=300) as resp:
                with dest.open("wb") as out:
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
                        total += len(chunk)
            return nf_id, str(dest), total
        except Exception as exc:
            last_exc = exc
            if dest.exists():
                dest.unlink(missing_ok=True)  # remove partial file
            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(DOWNLOAD_RETRY_WAIT * (attempt + 1))

    raise last_exc


def _db_status(db_path_str: str, nf_id: int, status: str) -> None:
    """Brief write to co_files from a worker thread/process."""
    con = sqlite3.connect(db_path_str)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("UPDATE co_files SET status=? WHERE nf_id=?", (status, nf_id))
    con.commit()
    con.close()


# ── Coordinator ───────────────────────────────────────────────────────────────

def _run_worker(db_path: Path) -> None:
    db_str = str(db_path)
    con    = sqlite3.connect(db_str)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")

    # Reset any rows left mid-state from a previously killed run
    con.execute(
        "UPDATE co_files SET status='queued' WHERE status IN ('downloading', 'parsing')"
    )
    con.commit()

    rows = con.execute(
        """
        SELECT cf.nf_id, nf.url, nf.url_path
        FROM   co_files cf
        JOIN   network_files nf ON nf.id = cf.nf_id
        WHERE  cf.status = 'queued'
        ORDER  BY cf.nf_id
        """
    ).fetchall()

    if not rows:
        con.close()
        return

    # parse pool stays open for the full duration so processes are reused
    with ProcessPoolExecutor(max_workers=PARSE_WORKERS) as parse_pool:
        parse_futures: dict = {}

        # Download pool: I/O bound, threads are sufficient
        with ThreadPoolExecutor(
            max_workers=DOWNLOAD_WORKERS, thread_name_prefix="co-dl"
        ) as dl_pool:
            dl_futures: dict = {
                dl_pool.submit(_download_one, r["nf_id"], r["url"], r["url_path"], db_str): r["nf_id"]
                for r in rows
            }

            # As each download completes, hand off to a parse process
            for dl_f in as_completed(dl_futures):
                nf_id = dl_futures[dl_f]
                try:
                    _, dest_str, file_bytes = dl_f.result()
                    con.execute(
                        "UPDATE co_files SET status='parsing', file_bytes=?, "
                        "downloaded_at=datetime('now'), local_path=? WHERE nf_id=?",
                        (file_bytes, dest_str, nf_id),
                    )
                    con.commit()
                    parse_futures[parse_pool.submit(_parse_file, dest_str)] = nf_id
                except Exception as exc:
                    con.execute(
                        "UPDATE co_files SET status='error', error=? WHERE nf_id=?",
                        (str(exc)[:400], nf_id),
                    )
                    con.commit()

        # Collect parse results as they finish
        for pf in as_completed(parse_futures):
            nf_id = parse_futures[pf]
            try:
                refs = pf.result()
                if refs:
                    con.executemany(
                        "INSERT INTO co_provider_refs (nf_id, provider_group_id, npi) "
                        "VALUES (?, ?, ?)",
                        [(nf_id, gid, npi) for gid, npi in refs],
                    )
                con.execute(
                    "UPDATE co_files SET status='done', pr_count=?, "
                    "parsed_at=datetime('now') WHERE nf_id=?",
                    (len(refs), nf_id),
                )
                con.commit()
            except Exception as exc:
                con.execute(
                    "UPDATE co_files SET status='error', error=? WHERE nf_id=?",
                    (f"parse: {exc}"[:400], nf_id),
                )
                con.commit()

    con.close()


def start_worker(db_path: Path) -> bool:
    """Start the background coordinator thread. Returns False if already running."""
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return False
        _worker = threading.Thread(
            target=_run_worker, args=(db_path,),
            daemon=True, name="co-worker",
        )
        _worker.start()
        return True


# ── Exploration ───────────────────────────────────────────────────────────────

def npi_lookup(con: sqlite3.Connection, npi: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT   pr.provider_group_id,
                 pr.nf_id,
                 nf.description,
                 nf.url_path,
                 cf.pr_count,
                 cf.file_bytes
        FROM     co_provider_refs pr
        JOIN     network_files nf ON nf.id = pr.nf_id
        JOIN     co_files      cf ON cf.nf_id = pr.nf_id
        WHERE    pr.npi = ?
        ORDER BY nf.description
        """,
        (npi,),
    ).fetchall()
    return [dict(r) for r in rows]


def load_rates(url_path: str, group_ids: set[str], limit: int = 300) -> list[dict]:
    gz = _dest(url_path)
    if not gz.exists():
        return []

    rates: list[dict] = []
    with _open_gz(gz) as f:
        for item in ijson.items(f, "in_network.item"):
            if len(rates) >= limit:
                break
            code  = item.get("billing_code", "")
            ctype = item.get("billing_code_type", "")
            name  = (item.get("name") or item.get("description", ""))[:80]
            for nr in item.get("negotiated_rates", []):
                pr_ids = {str(x) for x in nr.get("provider_references", [])}
                if not (pr_ids & group_ids):
                    continue
                for price in nr.get("negotiated_prices", []):
                    rates.append({
                        "billing_code":      code,
                        "billing_code_type": ctype,
                        "name":              name,
                        "negotiated_type":   price.get("negotiated_type", ""),
                        "negotiated_rate":   price.get("negotiated_rate"),
                        "billing_class":     price.get("billing_class", ""),
                        "service_code":      "|".join(price.get("service_code") or []),
                    })
                    if len(rates) >= limit:
                        break
    return rates
