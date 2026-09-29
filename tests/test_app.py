import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (
    QueryRequest, build_visualization, cache_key, fetch_studies, heuristic_intent,
    matches_filters, plan_query, render_metrics, retryable_status,
)
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import app as app_module

def study(nct, year, phase, drug="Drug A", sponsor="Acme", country="United States"):
    return {"protocolSection":{"identificationModule":{"nctId":nct,"briefTitle":"A trial"},"statusModule":{"startDateStruct":{"date":f"{year}-01-02"}},"designModule":{"phases":[phase]},"sponsorCollaboratorsModule":{"leadSponsor":{"name":sponsor}},"armsInterventionsModule":{"interventions":[{"name":drug,"type":"DRUG"}]},"contactsLocationsModule":{"locations":[{"country":country}]}}}

def test_time_series_has_citations():
    out=build_visualization([study("NCT1",2020,"PHASE2"),study("NCT2",2021,"PHASE3")],QueryRequest(query="trials per year"))
    assert out.type == "time_series" and out.data[0]["citations"][0]["nct_id"] == "NCT1"

def test_phase_distribution_and_year_filter():
    req=QueryRequest(query="phases",trial_phase=["PHASE2"],start_year=2020)
    assert matches_filters(study("NCT1",2020,"PHASE2"),req)
    assert not matches_filters(study("NCT2",2019,"PHASE2"),req)
    assert build_visualization([study("NCT1",2020,"PHASE2")],req).type == "bar_chart"

def test_network():
    out=build_visualization([study("NCT1",2020,"PHASE2")],QueryRequest(query="show sponsor drug network"))
    assert out.type == "network_graph" and len(out.data["edges"]) == 1

def test_dashboard_and_health_regression():
    root = Path(__file__).resolve().parents[1]
    page = (root / "static" / "index.html").read_text()
    js = (root / "static" / "app.js").read_text()
    assert 'TrialLens' in page and '/static/app.js' in page
    assert all(token in js for token in ('bar_chart', 'time_series', 'network_graph', 'citations'))

def test_ai_planner_safe_fallback_and_retry_policy(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    plan = asyncio.run(plan_query(QueryRequest(query="Tell me about melanoma research", condition="melanoma")))
    assert plan.source == "heuristic" and plan.intent == "intervention"
    assert retryable_status(429) and retryable_status(503) and not retryable_status(404)

def test_input_validation_rejects_out_of_contract_values():
    with pytest.raises(ValidationError):
        QueryRequest(query="ab")
    with pytest.raises(ValidationError):
        QueryRequest(query="x" * 501)
    with pytest.raises(ValidationError):
        QueryRequest(query="valid question", trial_phase=["PHASE9"])
    with pytest.raises(ValidationError):
        QueryRequest(query="valid question", start_year=1700)
    with pytest.raises(ValidationError):
        QueryRequest(query="valid question", end_year=2200)
    with pytest.raises(ValidationError):
        QueryRequest(query="valid question", max_studies=0)
    with pytest.raises(ValidationError):
        QueryRequest(query="valid question", max_studies=1001)
    assert QueryRequest(query="valid question", trial_phase=["PHASE2","NA"]).trial_phase == ["PHASE2","NA"]

def test_intent_selection_covers_every_supported_intent():
    assert heuristic_intent("sponsor drug network relationships") == "network"
    assert heuristic_intent("melanoma trials over time") == "time"
    assert heuristic_intent("trials per year by year") == "time"
    assert heuristic_intent("trials by country") == "country"
    assert heuristic_intent("trials by geographic location") == "country"
    assert heuristic_intent("phase 3 distribution") == "phase"
    assert heuristic_intent("what drugs are studied") == "intervention"

def test_retry_eligibility_covers_every_transient_status():
    for status in (429, 500, 502, 503, 504):
        assert retryable_status(status)
    for status in (200, 400, 401, 403, 404, 422):
        assert not retryable_status(status)
    assert not retryable_status(None)

def test_country_filter_is_case_insensitive():
    req = QueryRequest(query="trials in india", country="India")
    assert matches_filters(study("NCT1", 2020, "PHASE2", country="India"), req)
    assert matches_filters(study("NCT2", 2020, "PHASE2", country="india"), req)
    assert not matches_filters(study("NCT3", 2020, "PHASE2", country="France"), req)

class _StubTransport(httpx.AsyncBaseTransport):
    """Serves scripted ClinicalTrials.gov pages so pagination is testable offline."""
    def __init__(self, pages): self.pages, self.calls = pages, []
    async def handle_async_request(self, request):
        self.calls.append(request)
        token = request.url.params.get("pageToken")
        status, payload = self.pages[token] if token else self.pages[None]
        return httpx.Response(status, json=payload, request=request)

def test_fetch_studies_paginates_until_budget():
    pages = {
        None: (200, {"studies": [study("NCT1", 2020, "PHASE2")], "nextPageToken": "p2"}),
        "p2": (200, {"studies": [study("NCT2", 2021, "PHASE3")], "nextPageToken": "p3"}),
        "p3": (200, {"studies": [study("NCT3", 2022, "PHASE2")]}),
    }
    transport = _StubTransport(pages)
    original = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return original(*a, **kw)

    httpx.AsyncClient = patched
    app_module._cache.clear()
    try:
        req = QueryRequest(query="trials per year", max_studies=1000)
        out = asyncio.run(fetch_studies(req))
    finally:
        httpx.AsyncClient = original
    assert [s["protocolSection"]["identificationModule"]["nctId"] for s in out] == ["NCT1", "NCT2", "NCT3"]
    assert len(transport.calls) == 3

def test_fetch_studies_caches_second_identical_request():
    pages = {None: (200, {"studies": [study("NCT1", 2020, "PHASE2")]})}
    transport = _StubTransport(pages)
    original = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = transport
        return original(*a, **kw)

    httpx.AsyncClient = patched
    app_module._cache.clear()
    try:
        req = QueryRequest(query="cached question", condition="melanoma")
        first = asyncio.run(fetch_studies(req))
        second = asyncio.run(fetch_studies(req))
    finally:
        httpx.AsyncClient = original
    assert first == second
    assert len(transport.calls) == 1
    assert cache_key(req) in app_module._cache

def test_metrics_exposition_is_prometheus_shaped():
    text = render_metrics()
    for metric in ("trialens_http_requests_total", "trialens_http_request_duration_seconds",
                   "trialens_cache_hits_total", "trialens_upstream_requests_total", "trialens_cache_entries"):
        assert f"# TYPE {metric}" in text
    assert 'trialens_http_request_duration_seconds_bucket{le="+Inf"}' in text
    assert "trialens_http_request_duration_seconds_count" in text

def test_api_returns_request_id_and_structured_log(caplog):
    app_module._counters.clear()
    app_module._route_requests.clear()
    with TestClient(app_module.app) as client:
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        assert health.headers["X-Request-ID"]
        resp = client.get("/health", headers={"X-Request-ID": "test-request-id"})
        assert resp.headers["X-Request-ID"] == "test-request-id"
        metrics = client.get("/metrics")
        assert metrics.status_code == 200 and "trialens_http_requests_total" in metrics.text
        rejected = client.post("/v1/visualizations", json={"query": "ab"})
        assert rejected.status_code == 422
    assert "trialens_http_errors_total" in render_metrics()
