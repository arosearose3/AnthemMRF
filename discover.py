#!/usr/bin/env python3
"""
discover.py — Structural discovery for the Anthem MRF index file.

Streams the JSON without loading it into memory (uses ijson).
Makes two passes per file:
  Pass 1: reads top-level scalar metadata (terminates before the large array)
  Pass 2: iterates every reporting_structure entry to collect statistics

Outputs a console summary and writes discovery_report.json.

Usage:
    python discover.py                  # compressed file, then full 20 GB file
    python discover.py --compressed-only
    python discover.py --full-only
"""

import argparse
import gzip
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import ijson
from tqdm import tqdm

ROOT       = Path(__file__).parent
COMPRESSED = ROOT / "anthemmrfCompressed.json.gz"
FULL_INDEX = ROOT / "2026-06-01-AnthemData" / "2026-06-01_anthem_index.json"
REPORT_OUT = ROOT / "discovery_report.json"

# ── CMS TiC v2.0.0 field sets ─────────────────────────────────────────────────
CMS_TOP_LEVEL     = {"reporting_entity_name", "reporting_entity_type",
                     "last_updated_on", "version", "reporting_structure"}
CMS_PLAN_REQUIRED = {"plan_name", "plan_id_type", "plan_id", "plan_market_type"}
CMS_PLAN_OPTIONAL = {"plan_sponsor_name", "issuer_name"}
CMS_INF_REQUIRED  = {"description", "location"}
CMS_RS_EXPECTED   = {"reporting_plans", "in_network_files", "allowed_amount_files"}


def _open(path: Path):
    return gzip.open(path, "rb") if path.suffix == ".gz" else open(path, "rb")


def _read_top_level(path: Path) -> dict:
    """Read scalar fields that appear before reporting_structure (fast — exits early)."""
    meta = {}
    with _open(path) as f:
        for prefix, event, value in ijson.parse(f):
            if prefix == "reporting_structure" and event == "start_array":
                break
            if event in ("string", "number", "boolean") and prefix and "." not in prefix:
                meta[prefix] = value
    return meta


def _scan(path: Path, label: str) -> dict:
    rs_count   = 0
    plan_count = 0
    inf_count  = 0
    aaf_count  = 0

    plan_fields  = Counter()
    plan_missing = Counter()
    plan_extra   = Counter()
    plan_market  = Counter()
    plan_id_type = Counter()

    inf_fields  = Counter()
    inf_extra   = Counter()
    url_domains = Counter()
    presigned   = 0
    embedded    = 0
    rs_extra    = Counter()

    sample_plans = []
    sample_infs  = []
    sample_urls  = []

    with _open(path) as f:
        bar = tqdm(
            ijson.items(f, "reporting_structure.item"),
            desc=f"  {label}",
            unit=" entries",
            dynamic_ncols=True,
        )
        for entry in bar:
            rs_count += 1

            # Non-standard keys at the reporting_structure entry level
            for k in set(entry.keys()) - CMS_RS_EXPECTED:
                rs_extra[k] += 1

            # ── reporting_plans ──────────────────────────────────────────────
            for plan in entry.get("reporting_plans", []):
                plan_count += 1
                keys = set(plan.keys())

                for k in keys:
                    plan_fields[k] += 1
                for k in keys - CMS_PLAN_REQUIRED - CMS_PLAN_OPTIONAL:
                    plan_extra[k] += 1
                for k in CMS_PLAN_REQUIRED - keys:
                    plan_missing[k] += 1

                plan_market[plan.get("plan_market_type", "<missing>")] += 1
                plan_id_type[plan.get("plan_id_type", "<missing>")] += 1

                if len(sample_plans) < 3:
                    sample_plans.append(dict(plan))

            # ── in_network_files ─────────────────────────────────────────────
            for inf in entry.get("in_network_files", []):
                inf_count += 1
                keys = set(inf.keys())

                for k in keys:
                    inf_fields[k] += 1
                for k in keys - CMS_INF_REQUIRED:
                    inf_extra[k] += 1

                loc = inf.get("location", "")
                if isinstance(loc, str) and loc.startswith("http"):
                    url_domains[urlparse(loc).netloc] += 1
                    if any(p in loc for p in ("X-Amz-Signature", "Signature=", "Key-Pair-Id=", "se=", "token=")):
                        presigned += 1
                    if len(sample_urls) < 5:
                        sample_urls.append(loc)
                elif loc:
                    embedded += 1

                if len(sample_infs) < 3:
                    sample_infs.append({k: inf.get(k) for k in list(inf.keys())[:5]})

            # ── allowed_amount_files (optional CMS section) ──────────────────
            aaf_count += len(entry.get("allowed_amount_files", []))

            if rs_count % 500 == 0:
                bar.set_postfix(plans=plan_count, in_network=inf_count, refresh=False)

    def top_n(c: Counter, n: int = 20) -> dict:
        return dict(c.most_common(n))

    return {
        "reporting_structure_count":      rs_count,
        "total_reporting_plans":          plan_count,
        "total_in_network_files":         inf_count,
        "total_allowed_amount_files":     aaf_count,
        "plan_fields_present":            top_n(plan_fields),
        "plan_fields_missing_required":   top_n(plan_missing),
        "plan_extra_fields":              top_n(plan_extra),
        "plan_market_type_distribution":  top_n(plan_market),
        "plan_id_type_distribution":      top_n(plan_id_type),
        "in_network_fields_present":      top_n(inf_fields),
        "in_network_extra_fields":        top_n(inf_extra),
        "in_network_url_domains":         top_n(url_domains),
        "in_network_presigned_count":     presigned,
        "in_network_embedded_data_count": embedded,
        "reporting_structure_extra_keys": top_n(rs_extra),
        "sample_plans":                   sample_plans,
        "sample_in_network_files":        sample_infs,
        "sample_urls":                    sample_urls,
    }


def run_file(path: Path, label: str) -> dict:
    size = path.stat().st_size
    print(f"\n{'─' * 62}")
    print(f"  {label}  —  {size / 1e6:,.0f} MB  |  {path.name}")
    print(f"{'─' * 62}")

    t0      = time.time()
    meta    = _read_top_level(path)
    stats   = _scan(path, label)
    elapsed = time.time() - t0

    always_missing_plan = {
        k: v for k, v in stats["plan_fields_missing_required"].items()
        if stats["total_reporting_plans"] > 0 and v == stats["total_reporting_plans"]
    }

    return {
        "source":       str(path),
        "label":        label,
        "file_bytes":   size,
        "scan_seconds": round(elapsed, 1),
        **meta,
        **stats,
        "conformance": {
            "top_level_missing":          sorted(CMS_TOP_LEVEL - set(meta.keys()) - {"reporting_structure"}),
            "top_level_extra":            sorted(set(meta.keys()) - CMS_TOP_LEVEL),
            "plan_fields_always_missing": always_missing_plan,
        },
    }


def print_summary(r: dict) -> None:
    print(f"\n  Entity  : {r.get('reporting_entity_name')}  ({r.get('reporting_entity_type')})")
    print(f"  Updated : {r.get('last_updated_on')}    Version : {r.get('version')}")
    print()
    print(f"  reporting_structure entries :  {r['reporting_structure_count']:>10,}")
    print(f"  Total reporting_plans       :  {r['total_reporting_plans']:>10,}")
    print(f"  Total in_network_files      :  {r['total_in_network_files']:>10,}")
    print(f"  Total allowed_amount_files  :  {r['total_allowed_amount_files']:>10,}")
    print(f"  Scan time                   :  {r['scan_seconds']}s")
    print()

    c = r["conformance"]
    if c["top_level_missing"]:
        print(f"  [FAIL] Missing required top-level fields  : {c['top_level_missing']}")
    else:
        print("  [PASS] All required top-level fields present")
    if c["top_level_extra"]:
        print(f"  [NOTE] Non-standard top-level fields      : {c['top_level_extra']}")

    if c["plan_fields_always_missing"]:
        print(f"  [FAIL] Required plan fields never present : {list(c['plan_fields_always_missing'].keys())}")
    elif r["plan_fields_missing_required"]:
        print(f"  [WARN] Required plan fields sometimes missing : {r['plan_fields_missing_required']}")
    else:
        print("  [PASS] All required plan fields present in every entry")

    print()
    print(f"  Plan market types : {r['plan_market_type_distribution']}")
    print(f"  Plan ID types     : {r['plan_id_type_distribution']}")
    print()

    total = r["total_in_network_files"]
    pre   = r["in_network_presigned_count"]
    print("  in_network URL domains:")
    for domain, count in list(r["in_network_url_domains"].items())[:5]:
        print(f"    {count:>8,}  {domain}")
    pct = 100 * pre / total if total else 0
    print(f"  Pre-signed URLs : {pre:,} / {total:,}  ({pct:.1f}%)")
    if r["in_network_embedded_data_count"]:
        print(f"  [WARN] Non-URL location values  : {r['in_network_embedded_data_count']:,}")

    if r["plan_extra_fields"]:
        print(f"\n  Non-standard plan fields         : {r['plan_extra_fields']}")
    if r["in_network_extra_fields"]:
        print(f"  Non-standard in_network fields   : {r['in_network_extra_fields']}")
    if r["reporting_structure_extra_keys"]:
        print(f"  Non-standard reporting_structure : {r['reporting_structure_extra_keys']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Anthem MRF structural discovery")
    parser.add_argument("--file", type=Path, default=None,
                        help="Path to the full index JSON file (overrides the default hardcoded path)")
    parser.add_argument("--compressed-only", action="store_true",
                        help="Only scan the compressed file (fast, ~30 seconds)")
    parser.add_argument("--full-only", action="store_true",
                        help="Only scan the full index file (10–30 minutes)")
    parsed = parser.parse_args()

    full_index     = parsed.file or FULL_INDEX
    run_compressed = not parsed.full_only
    run_full       = not parsed.compressed_only

    results: dict = {}

    if run_compressed:
        if not COMPRESSED.exists():
            print(f"[skip] Not found: {COMPRESSED}")
        else:
            results["compressed"] = run_file(COMPRESSED, "Compressed file")
            print_summary(results["compressed"])

    if run_full:
        if not full_index.exists():
            print(f"[skip] Not found: {full_index}")
        else:
            print("\n  Full scan — this will take 10–30 minutes depending on disk speed.")
            results["full"] = run_file(full_index, "Full index")
            print_summary(results["full"])

    REPORT_OUT.write_text(json.dumps(results, indent=2, default=str))
    print(f"  Report written → {REPORT_OUT}\n")


if __name__ == "__main__":
    main()
