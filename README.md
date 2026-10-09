# Kira-style Contract Heatmap

A prototype for comparing contracts against a form/reference agreement using the Claude API (Anthropic).

## What it does

1. Upload a PDF form agreement.
2. Upload one or more PDF contracts.
3. Claude extracts predefined provisions from the reference, then from each contract independently.
4. Claude compares each contract's extracted provisions against the cached reference extraction.
5. Streamlit renders a live-updating progress panel, an interactive heatmap, reviewer overrides, and clause-level explanations.

## Setup (Windows PowerShell)

Python 3.10+ is recommended.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set your Anthropic API key:

```text
ANTHROPIC_API_KEY=your_anthropic_api_key_here
ANTHROPIC_MODEL=claude-sonnet-5-5
DEMO_MODE=false
```

Run:

```powershell
streamlit run app.py
```

Then open the local Streamlit URL shown in the terminal.

> If `Activate.ps1` is blocked by PowerShell's execution policy, run:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`

## Demo mode (no API calls)

Set `DEMO_MODE=true` in `.env`, or for a single session:

```powershell
$env:DEMO_MODE = "true"
streamlit run app.py
```

With `DEMO_MODE=true`:

- Claude is never called.
- Extraction and comparison results are generated deterministically (seeded from each PDF's SHA-256 hash), so the same file always produces the same mock result.
- The UI displays a prominent **"DEMO MODE — Claude API calls disabled"** banner.
- The full interface (upload, progress, retry, heatmap, reviewer overrides) can be exercised without an API key or network access.

Set `DEMO_MODE=false` (or remove it) to use the real Claude API.

## Running with the real Claude API

```powershell
$env:DEMO_MODE = "false"
streamlit run app.py
```

Make sure `ANTHROPIC_API_KEY` is set in `.env`. `ANTHROPIC_MODEL` controls the default model (never hard-coded); it can also be overridden per-session from the sidebar.

## Reliability features

- **Retry with backoff**: Transient Claude errors — `overloaded_error`/503, other `5xx`, and `429` rate limits — are retried automatically: 1 initial attempt plus up to 4 retries (5 calls max), waiting 2s, 4s, 8s, then 16s between attempts. Authentication errors, invalid-model errors, and malformed/invalid-request errors (4xx other than 429) are never retried. The SDK's own built-in retry is disabled (`max_retries=0`) so this backoff schedule has exclusive control.
- **Partial-batch resilience**: If one contract fails, the rest of the batch still runs. Successful contracts appear in the heatmap; failed ones are listed separately.
- **Retry failed documents**: A dedicated button reprocesses only the reference (if it failed) and the contracts that failed, without re-running anything that already succeeded.
- **Local caching**: Every successful extraction and comparison is cached on disk, keyed by the SHA-256 hash of the PDF content, the Claude model name, and an internal schema version. Re-uploading an already-analyzed PDF (with the same model) loads the cached result instead of calling Claude again.
- **Live progress panel**: Each document shows its state as it is processed, e.g.:

  ```text
  Reference Agreement ✓

  Contract 1 ✓
  Contract 2 ✓
  Contract 3 ⚠ Retrying...
  Contract 4 ✓
  Contract 5 ❌ Failed
  ```

## Clearing the cache

From the app sidebar, click **"Clear local cache"**.

From PowerShell:

```powershell
Remove-Item -Recurse -Force .cache
```

(The cache directory is `.cache/` by default, or wherever `KIRA_CACHE_DIR` points if you set that environment variable.)

## Reviewer overrides

Every clause comparison can be overridden by a reviewer:

- Status: Same, Modified, Major Deviation, Missing, Added
- An optional free-text note

Overrides are reflected immediately in the heatmap. The underlying Claude-assigned status and explanation are left untouched and still shown — the override is an additional reviewer judgment layered on top.

## Numeric deviation detection

Independently of Claude's semantic classification, the app deterministically scans each clause's form/contract text for comparable numeric values — dollar amounts, percentages, and day/month/year durations (covering liability caps, insurance limits, and notice periods) — and generates an explicit explanation, for example:

```text
Liability cap decreased from $1,000,000 in the form to $500,000 in the contract.
```

If Claude classified such a clause as "same" but the numbers actually differ, the status is upgraded to "modified".

## Architecture

```text
Reference PDF
  |
  v
Claude extraction (tool-use, structured output)
  |
  v
Cached reference result (SHA-256 + model + schema version)

Each Contract PDF
  |
  v
Claude extraction (tool-use, structured output)
  |
  v
Cached contract result (SHA-256 + model + schema version)

Cached reference + cached contract
  |
  v
Claude comparison (+ deterministic numeric-deviation check)
  |
  v
Deterministic status -> color mapping
  |
  v
Kira-style heatmap + reviewer overrides
```

PDFs are sent to Claude as inline base64 `document` content blocks. Structured
output is obtained by forcing a single tool call (`tool_choice`) whose
`input_schema` is generated directly from the Pydantic models in
`analyzer.py`, so the parsed response is guaranteed to match the expected
shape without a separate JSON-parsing step.

## Important prototype limitations

This is a review-assistance prototype, not a legal decision system.

- Clause taxonomy is currently fixed in `CLAUSES`.
- Page citations come from Claude's document reading and should be verified.
- "Major deviation" is primarily an LLM classification; only numeric-value changes are independently verified in Python.
- PDFs are sent inline as base64 (no separate upload/Files API), so very large PDFs may need chunking in a production version.
- No OCR fallback is implemented outside Claude's native PDF support.
- No user authentication, document persistence, or audit trail beyond the session is included.
- The on-disk cache stores extracted clause text and comparison results in plaintext JSON — do not point `KIRA_CACHE_DIR` at a shared or non-private location if contracts are confidential.
- Do not upload confidential contracts unless your Claude/API configuration and organizational policies permit it.

## Next production steps

- Add user-defined clause templates.
- Store normalized clause embeddings and extracted clauses in a database instead of flat JSON cache files.
- Add side-by-side PDF page viewer.
- Persist reviewer overrides and a full audit trail beyond the Streamlit session.
- Extend deterministic numeric-deviation detection to more unit types and multi-value clauses.
- Add DOCX support.
- Add batch jobs/queues for hundreds or thousands of contracts.
- Add database-backed projects and permissions.
