#!/usr/bin/env python3
"""
app.py — Anthem MRF Explorer web application.

Requires anthem_index.db. Build it first:
    python indexer.py

Then start the server:
    python app.py

Open http://localhost:5000
"""

import sqlite3
from pathlib import Path

import colorado as co
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for

ROOT     = Path(__file__).parent
DB_PATH  = ROOT / "anthem_index.db"
PER_PAGE = 50

app = Flask(__name__)


# ── Database connection ───────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        con = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        con.row_factory = sqlite3.Row
        g.db = con
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    con  = get_db()
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}

    stats = {
        "plan_count":   con.execute("SELECT COUNT(*) FROM plans").fetchone()[0],
        "nf_count":     con.execute("SELECT COUNT(*) FROM network_files").fetchone()[0],
        "rs_count":     int(meta.get("rs_count", 0)),
        "entity_name":  meta.get("reporting_entity_name", "Anthem Inc"),
        "last_updated": meta.get("last_updated_on", "?"),
        "version":      meta.get("version", "?"),
        "built_at":     meta.get("index_built_at", "?"),
    }

    domains = con.execute(
        "SELECT domain, COUNT(*) AS cnt FROM network_files GROUP BY domain ORDER BY cnt DESC"
    ).fetchall()

    return render_template("index.html", stats=stats, domains=domains)


@app.route("/search")
def search():
    q      = request.args.get("q", "").strip()
    field  = request.args.get("field", "sponsor")
    market = request.args.get("market", "")
    page   = max(1, int(request.args.get("page", 1) or 1))

    if not q:
        return redirect(url_for("index"))

    con  = get_db()
    like = f"%{q}%"

    col = {
        "sponsor": "plan_sponsor_name",
        "name":    "plan_name",
        "plan_id": "plan_id",
    }.get(field, "plan_sponsor_name")

    where  = f"{col} LIKE ? COLLATE NOCASE"
    params: list = [like]

    if market in ("group", "individual"):
        where += " AND plan_market_type = ?"
        params.append(market)

    total  = con.execute(f"SELECT COUNT(*) FROM plans WHERE {where}", params).fetchone()[0]
    offset = (page - 1) * PER_PAGE
    rows   = con.execute(
        f"SELECT * FROM plans WHERE {where} "
        f"ORDER BY plan_sponsor_name COLLATE NOCASE, plan_name COLLATE NOCASE "
        f"LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset],
    ).fetchall()

    return render_template(
        "search.html",
        rows=rows,
        q=q,
        field=field,
        market=market,
        total=total,
        page=page,
        per_page=PER_PAGE,
        pages=(total + PER_PAGE - 1) // PER_PAGE,
    )


@app.route("/plan/<int:plan_id>")
def plan_detail(plan_id: int):
    con  = get_db()
    plan = con.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not plan:
        abort(404)

    siblings = con.execute(
        "SELECT * FROM plans WHERE rs_idx = ? AND id != ? ORDER BY plan_name COLLATE NOCASE",
        (plan["rs_idx"], plan_id),
    ).fetchall()

    network_files = con.execute(
        """
        SELECT nf.id, nf.description, nf.domain, nf.url_path, nf.url
        FROM   network_files nf
        JOIN   rs_network_files rsnf ON rsnf.nf_id = nf.id
        WHERE  rsnf.rs_idx = ?
        ORDER  BY nf.domain, nf.description
        """,
        (plan["rs_idx"],),
    ).fetchall()

    return render_template(
        "plan.html",
        plan=plan,
        siblings=siblings,
        network_files=network_files,
    )


@app.route("/network-files")
def network_files():
    con    = get_db()
    domain = request.args.get("domain", "")
    desc   = request.args.get("desc", "").strip()
    page   = max(1, int(request.args.get("page", 1) or 1))

    where:  list[str] = []
    params: list      = []

    if domain:
        where.append("domain = ?")
        params.append(domain)
    if desc:
        where.append("description LIKE ? COLLATE NOCASE")
        params.append(f"%{desc}%")

    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total  = con.execute(f"SELECT COUNT(*) FROM network_files {clause}", params).fetchone()[0]
    offset = (page - 1) * PER_PAGE
    rows   = con.execute(
        f"SELECT * FROM network_files {clause} "
        f"ORDER BY domain, description LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset],
    ).fetchall()

    domains = con.execute(
        "SELECT domain, COUNT(*) AS cnt FROM network_files GROUP BY domain ORDER BY cnt DESC"
    ).fetchall()

    return render_template(
        "network_files.html",
        rows=rows,
        domain=domain,
        desc=desc,
        domains=domains,
        total=total,
        page=page,
        per_page=PER_PAGE,
        pages=(total + PER_PAGE - 1) // PER_PAGE,
    )


# ── Colorado ──────────────────────────────────────────────────────────────────

@app.route("/colorado")
def colorado():
    con = get_db()
    co.init_tables(con)
    counts = co.file_counts(con)
    errors = co.recent_errors(con) if counts["errors"] else []
    return render_template("colorado.html", counts=counts, errors=errors)


@app.route("/colorado/start", methods=["POST"])
def colorado_start():
    con = get_db()
    co.init_tables(con)
    co.queue_all(con)
    co.start_worker(DB_PATH)
    return redirect(url_for("colorado"))


@app.route("/colorado/api/status")
def colorado_api_status():
    con = get_db()
    return jsonify(co.file_counts(con))


@app.route("/colorado/api/npi/<path:npi>")
def colorado_api_npi(npi: str):
    con = get_db()
    return jsonify(co.npi_lookup(con, npi.strip()))


@app.route("/colorado/api/rates")
def colorado_api_rates():
    npi_val  = request.args.get("npi", "").strip()
    nf_id    = request.args.get("nf_id", type=int)
    url_path = request.args.get("url_path", "")
    if not npi_val or not nf_id or not url_path:
        return jsonify({"error": "npi, nf_id, url_path required"}), 400

    con      = get_db()
    gid_rows = con.execute(
        "SELECT DISTINCT provider_group_id FROM co_provider_refs WHERE npi=? AND nf_id=?",
        (npi_val, nf_id),
    ).fetchall()
    group_ids = {r[0] for r in gid_rows}
    rates     = co.load_rates(url_path, group_ids)
    return jsonify({"rates": rates, "truncated": len(rates) >= 300})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH.name}")
        print("Build it first:  python indexer.py")
        raise SystemExit(1)
    print("Starting Anthem MRF Explorer at http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
