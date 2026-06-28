# Anthem MRF Explorer — Complete How-To

## What this is

A locally-hosted Python web app that lets you search and browse the Anthem Machine Readable File (MRF) — a 20 GB JSON index published monthly under the CMS Transparency in Coverage rule. The index maps employer health plans to their in-network rate files.

---

## Prerequisites

- Python 3.10 or later
- ~25 GB free disk space (20 GB raw file + 2–3 GB SQLite database)

---

## First-time setup

### 1. Install Python dependencies

```
pip install -r requirements.txt
```

Only needs to be done once (or after a fresh Python install).

### 2. Get the Anthem MRF index file

Go to Anthem's MRF page and download the index file for the current month. Anthem publishes it around the 1st of each month.

https://www.anthem.com/machine-readable-file/search/

The file is a `.json.gz` (compressed) or `.json` (uncompressed). The naming convention is:

```
YYYY-MM-DD_anthem_index.json
```

Save it inside a dated subfolder so old versions are easy to keep or remove:

```
anthemMRF\
  2026-06-01-AnthemData\
    2026-06-01_anthem_index.json       ← 20 GB
```

There may also be a compressed `.json.gz` copy of the same file at ~100 MB. Keep it alongside the full file if you want the fast sanity-check scan.

### 3. Profile the file (optional but recommended)

`discover.py` streams the file and checks it against the CMS spec. Takes 3–10 minutes:

```
python discover.py --file "2026-06-01-AnthemData\2026-06-01_anthem_index.json"
```

Output goes to `discovery_report.json`. Read the console summary — look for any `[FAIL]` lines, which indicate Anthem deviated from the spec in a way that may break the indexer.

For a 30-second sanity check using the compressed file (if present):

```
python discover.py --compressed-only
```

### 4. Build the SQLite index

This is the slow step. It reads the entire 20 GB file once and writes a queryable database:

```
python indexer.py --file "2026-06-01-AnthemData\2026-06-01_anthem_index.json"
```

**Expected time: 20–40 minutes.** A progress bar shows entries/second. The output file is `anthem_index.db` in the project root.

When it finishes it prints the database size and the count of unique network files found.

### 5. Start the web app

```
python app.py
```

Open **http://localhost:5000** in a browser. Stop with `Ctrl+C`.

---

## Monthly update — new file from Anthem

Anthem publishes a new index file around the 1st of each month. Here is the full update procedure:

### Step 1 — Download the new file

Download the new index from Anthem's MRF page. Place it in a new dated folder:

```
anthemMRF\
  2026-08-01-AnthemData\
    2026-08-01_anthem_index.json       ← new file
  2026-06-01-AnthemData\
    2026-06-01_anthem_index.json       ← old file (keep until new one is verified)
```

### Step 2 — Profile the new file

```
python discover.py --file "2026-08-01-AnthemData\2026-08-01_anthem_index.json"
```

Check the console output for `[FAIL]` or `[WARN]` lines. Compare counts to the previous run in `discovery_report.json`. Large changes in plan count or network file count are worth investigating before committing to a 40-minute index rebuild.

### Step 3 — Delete the old database

```
del anthem_index.db
```

The indexer refuses to overwrite an existing database.

### Step 4 — Build the new index

```
python indexer.py --file "2026-08-01-AnthemData\2026-08-01_anthem_index.json"
```

Same 20–40 minute wait. Output is a new `anthem_index.db`.

### Step 5 — Start the app and verify

```
python app.py
```

Open http://localhost:5000 and check that the home page shows the new `last_updated_on` date and the plan/file counts look reasonable. Search for a plan you know and confirm the results.

Once verified, you can delete the old data folder to reclaim the 20 GB:

```
rmdir /s /q "2026-06-01-AnthemData"
```

---

## Using the web app

### Search plans

The home page has a search box. Search by:

- **Plan Sponsor (Employer)** — the company whose employees are covered. Most useful starting point. Try a company name like `"Amazon"` or `"Boeing"`.
- **Plan Name** — the insurance product name (e.g., `"PPO"`, `"HMO"`).
- **Plan ID** — the EIN or HIOS identifier if you have it.

Filter by market type (group = employer plans, individual = marketplace plans). Results paginate 50 per page.

### Plan detail page

Click any plan to see:

- All metadata fields for that plan
- **Sibling plans** — other insurance products from the same employer that share a reporting-structure entry (often the full list of plans an employer offers)
- **Network files** — every rate file linked to this plan. Each row shows the network description (e.g., `"BCBS Alabama : PAR Network"`), the hosting domain (which BCBS state affiliate), the filename, and a download link

The download links are pre-signed CloudFront URLs. They are valid for approximately one year from the file generation date.

### Network files browser

`/network-files` lets you browse all unique rate files. Use the domain chips at the top to filter to one state (e.g., `anthembcbsoh` = Anthem Blue Cross Blue Shield Ohio). Use the description filter to find a specific network by name.

---

## Colorado rate data explorer

The `/colorado` page is a focused tool for downloading and exploring Anthem's Colorado rate files. All other pages work from the SQLite index alone; the Colorado page is the one place where the app actually downloads external rate files to disk.

### Purpose

There are 715 unique rate files hosted on `anthembcbsco.mrf.bcbs.com`. These contain the actual negotiated prices Anthem pays Colorado providers for specific procedure codes. The index only tells you which files exist — you have to download them to see rates.

### First visit

Navigate to **http://localhost:5000/colorado**. On the first visit the app creates two extra tables in `anthem_index.db`:

- `co_files` — one row per Colorado rate file, tracking download status
- `co_provider_refs` — provider group / NPI mappings extracted from each downloaded file

No files are downloaded automatically. The page shows a status dashboard and a **Start Download** button.

### Starting the download

Click **Start Download**. The app will ask you to confirm (715 files, potentially hundreds of GB). On confirmation it:

1. Queues all 715 Colorado files in `co_files`
2. Starts a background worker thread that loops: pick next queued file → download to `data/co/` → parse `provider_references` → mark done → repeat

The page auto-refreshes its status cards every 5 seconds while the worker runs. You can navigate away and return — the worker continues as long as `python app.py` is running.

### Persistence across restarts

Downloaded files are stored in `data/co/` as `.json.gz` files. If you stop the server and restart it, files that were already downloaded are not re-downloaded — the worker detects the file on disk and skips straight to parsing. Click **Start Download** again after a restart to resume where it left off.

### Disk space

Each Colorado rate file is typically 50 MB – 2 GB compressed. Plan for up to several hundred GB total. The `data/co/` folder is created automatically the first time the Colorado page is loaded.

### NPI provider lookup

Once some files have been parsed, a search box appears. Enter a 10-digit NPI to find every Colorado network file that contains that provider. Results show:

- The network description (e.g., `"BCBS Colorado : BlueChoice Network"`)
- The filename
- How many provider references are in that file
- The compressed file size

Each result row has a **Load Rates** button.

### Loading rates

Clicking **Load Rates** streams the downloaded `.json.gz` file on-demand and returns up to 300 negotiated rate rows for that provider. Columns:

| Column | Description |
|--------|-------------|
| Code | CPT / HCPCS / DRG / etc. billing code |
| Type | Code type (CPT, HCPCS, DRG, …) |
| Description | Procedure name |
| Rate Type | `negotiated` / `derived` / `fee schedule` / `per diem` |
| Rate ($) | Dollar amount Anthem pays |
| Billing Class | `professional` or `institutional` |
| Service Code | Place-of-service codes (pipe-separated) |

Large files may take several minutes to stream. The page shows a spinner during loading. If the result is truncated at 300 rows a warning is shown — the provider has more rates in that file than are displayed.

### Error handling

If a file fails to download or parse, its row in `co_files` is marked `error` and the error message is stored. The **Recent Errors** section at the bottom of the page lists the last 10 failures. The worker skips errored files and continues with the rest of the queue.

---

## CMS conformance notes for the June 2026 file

| Check | Result |
|-------|--------|
| Required top-level fields | PASS |
| `plan_name` present | WARN — missing in 1,746 / 681,426 plans (0.3%) |
| `allowed_amount_files` (out-of-network data) | WARN — Anthem used `allowed_amount_file` (singular); spec requires plural. Not indexed. |
| Pre-signed URL format | NOTE — CloudFront auth (`Signature=` / `Key-Pair-Id=`), valid ~June 2027 |
| Non-standard extra fields | None |

Run `discover.py` on any new file to regenerate this check.

---

## Troubleshooting

**`anthem_index.db` already exists**
Delete it before running `indexer.py`. The indexer will not overwrite.

**`[skip] Not found`** from discover.py
The `--file` path doesn't exist. Check the folder name and filename match exactly.

**Indexer runs slowly or stalls**
Normal behavior — it is writing 40+ million rows to SQLite. The progress bar may appear to slow during the index-build phase at the end (after the streaming finishes). Let it run.

**URL downloads return 403 Forbidden**
The pre-signed CloudFront links have expired. This means you are using an old index file. Download the current month's file from Anthem.

**Search returns no results**
SQLite LIKE search is case-insensitive but requires the string to be somewhere in the field. Try a shorter search term (e.g., `"boeing"` instead of `"boeing employees"` — the plan sponsor field may not include "employees").

---

## File layout

```
anthemMRF\
├── discover.py              # CMS conformance + structural profile tool
├── indexer.py               # one-time SQLite builder (run per new file)
├── app.py                   # Flask web server
├── colorado.py              # Colorado download worker and exploration logic
├── requirements.txt         # pip dependencies
├── how-to.md                # this file
├── discovery_report.json    # output of discover.py (overwritten each run)
├── anthem_index.db          # output of indexer.py (also stores co_files, co_provider_refs)
├── anthemmrfCompressed.json.gz   # optional compressed file for quick scans
├── data\
│   └── co\                  # downloaded Colorado rate files (.json.gz), created on demand
├── templates\
│   ├── base.html
│   ├── index.html
│   ├── search.html
│   ├── plan.html
│   ├── network_files.html
│   └── colorado.html
└── 2026-06-01-AnthemData\
    └── 2026-06-01_anthem_index.json   # 20 GB source file
```
