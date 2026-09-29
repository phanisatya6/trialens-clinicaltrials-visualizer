# ClinicalTrials.gov Query-to-Visualization Agent

An API-only backend that turns a natural-language question plus optional filters into a frontend-ready visualization specification. It queries the official ClinicalTrials.gov API v2 at request time and returns only data supported by retrieved records.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Then open `http://127.0.0.1:8000/` for the interactive dashboard, `http://127.0.0.1:8000/docs` for Swagger UI, or `http://127.0.0.1:8000/metrics` for Prometheus metrics. Run `python run_examples.py` after the server starts. Run tests with `pytest -q` (13 tests).

The frontend uses responsive, dependency-free SVG charts. It supports time-series, bar-chart, and sponsor–drug network responses, hover tooltips, example queries, record-level citations, loading/errors, and JSON download.

Container run: `docker build -t clinicaltrials-viz . && docker run -p 8000:8000 clinicaltrials-viz`.

## Request schema

`POST /v1/visualizations` accepts `query` (required, 3–500 characters) and optional `drug_name`, `condition`, `trial_phase` (`PHASE1`, `PHASE2`, `PHASE3`, `PHASE4`, `EARLY_PHASE1`, `NA`), `sponsor`, `country`, `start_year`, `end_year`, and `max_studies` (1–1000). Inputs are type- and range-validated.

## Response schema

The top-level response contains `visualization` and `meta`. Visualization contains `type` (`bar_chart`, `time_series`, or `network_graph`), `title`, `encoding`, and `data`. Each aggregate datum or graph edge has `citations`, with an NCT ID, API-backed supporting excerpt, and canonical study URL. `meta` provides source, applied filters, the deterministic interpretation, record count, `request_id`, `cache.hit`, `duration_ms`, the page budget, and limitations.

## Reliability and observability

ClinicalTrials.gov is an external API, so the service is built for transient failure:

- **Pagination** — follows `nextPageToken` up to `MAX_PAGES` (10) pages, stopping at the `max_studies` budget. Reaching the page budget is counted in `trialens_upstream_pages_truncated_total` and logged.
- **Retries** — retries `429`, `500`, `502`, `503`, `504`, and transport errors three times with exponential backoff (`0.5s`, `1s`, `2s`). Non-retryable statuses fail immediately rather than burning the budget.
- **Cache** — identical filter sets are served from an in-process TTL cache keyed by a hash of the normalized request. TTL comes from `CACHE_TTL_SECONDS` (default 300). A cache hit returns byte-identical output.
- **Request IDs** — every response carries `X-Request-ID`, echoing an inbound header when supplied and generating a UUID otherwise.
- **Structured logs** — one JSON object per line on stdout, with `event`, `request_id`, `status`, and `duration_ms`.
- **Metrics** — `/metrics` exposes Prometheus text format: request/error counts by route, a latency histogram, upstream call/retry/truncation counts, cache hits, misses, and entries, and studies retrieved.

## Design

The planner is deterministic and keyword-based: time language produces a yearly series; geographic/phase/distribution language produces a bar chart; relationship language produces a sponsor–drug graph. This prevents an LLM from silently inventing query parameters or chart values. Retrieval uses documented API v2 search parameters, follows `nextPageToken` within an explicit page budget, then applies local exact filtering for year, phase, and country. Results are capped by `max_studies` and `MAX_PAGES` to keep response cost and latency bounded, and both limits are reported in `meta` and in the response `limitations` field.

## AI planner and MCP tool

By default the deterministic planner runs with no secret or AI dependency. To enable the optional AI planner, set `OPENAI_API_KEY`, `AI_PLANNER=openai`, and optionally `AI_MODEL`. The AI is constrained to return one of six chart intents and a confidence score. It cannot create filters, counts, source records, or citations; the API and deterministic aggregators remain the only factual path. If the model is unavailable or returns invalid JSON, the service safely falls back to the heuristic planner.

`mcp_server.py` is an optional stdio MCP server exposing `search_clinical_trials` as a tool for an MCP client. It shares the retrieval path, so it inherits the same pagination, retry, and cache behaviour. `mcp` is already included in `requirements.txt`; run it with `python mcp_server.py`. In production it should be installed in a dedicated environment, separate from the web service.

## Tradeoffs and next steps

Retrieval is bounded twice, by `max_studies` and by a 10-page budget, so counts describe retrieved records rather than the whole study pool; the response says so explicitly. The retry and cache layers are in-process, which suits a single container but would need a shared cache and a request-wide budget for horizontal scaling. `MAX_PAGES` is a module constant rather than a per-request field. This submission chooses inspectable constrained planning over an opaque model-driven workflow.

## Test coverage

`pytest -q` runs 13 tests covering the decision points named in the design: intent selection across all supported intents, input validation (length bounds, phase enum, year and `max_studies` ranges), citations, phase/year/country filtering, network generation, AI fallback when no key is present, retry eligibility for every transient and non-transient status, pagination across multiple `nextPageToken` pages, cache identity on a repeated request, Prometheus exposition shape, request-ID propagation, and frontend asset regressions.

## AI-tool disclosure

AI assistance was used to accelerate initial scaffolding and documentation. The final design, data model, retrieval/filtering behavior, citation model, and tests were reviewed and adapted deliberately. Verification includes unit tests and live calls to the official endpoint.
