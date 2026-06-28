# Anthem MRF Explorer

## Project goal

Build a locally-hosted Python web app to explore the Anthem Machine Readable File (MRF), a ~20 GB JSON file produced under CMS Transparency in Coverage rules.

## Data

| File | Size | Notes |
|------|------|-------|
| `2026-06-01-AnthemData/2026-06-01_anthem_index.json` | ~20 GB | Main index file |
| `anthemmrfCompressed.json.gz` | ~107 MB | Compressed version |

### File format (CMS TiC v2.0.0)

The index file is a single large JSON object:

```json
{
  "reporting_entity_name": "Anthem Inc",
  "reporting_entity_type": "health insurance issuer",
  "last_updated_on": "2026-06-01",
  "version": "2.0.0",
  "reporting_structure": [
    {
      "reporting_plans": [
        {
          "plan_name": "...",
          "plan_id_type": "EIN",
          "issuer_name": "Anthem Inc",
          "plan_id": "...",
          "plan_sponsor_name": "...",
          "plan_market_type": "group"
        }
      ],
      "in_network_files": [
        {
          "description": "BCBS Alabama : PAR Network",
          "location": "https://anthembcca.mrf.bcbs.com/..."
        }
      ]
    }
  ]
}
```

- `reporting_structure` is a large array; each element links one or more plans to external in-network rate files.
- `in_network_files[].location` URLs are pre-signed, time-limited links to gzipped JSON files hosted on Anthem/BCBS servers.
- The external files (not included locally) contain actual negotiated rates per procedure code and provider.

## Key constraints

- **Cannot load the full file into memory.** 20 GB JSON must be streamed or indexed. Use `ijson` for streaming parsing.
- The file is local on this Windows 11 machine; the app is accessed via `localhost`.
- Python web stack — no framework preference specified; Flask or FastAPI are reasonable choices.

## Architecture guidance

- Stream-parse the index file with `ijson` to build a lightweight SQLite index (plan names, sponsor names, network descriptions, file locations) on first run.
- Serve the explorer UI from that SQLite index — never re-read the 20 GB file for queries.
- External in-network rate files are behind pre-signed URLs; downloading them is optional/on-demand.
