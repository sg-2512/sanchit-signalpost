"""NAV feed candidates never become claims without a live exact-employer detail."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from norway_company_agent.connectors import nav_feed
from norway_company_agent.source_policy import observation_decision
from norway_company_agent.verified_items import validate_job_item


class _Budget:
    def can_proceed(self, **_: object) -> bool:
        return True


def test_live_detail_rejects_other_employer_and_inactive(monkeypatch) -> None:
    company = "FJORD TEKNOLOGI AS"
    url = "https://pam-stilling-feed.nav.no/api/v1/feedentry/11111111-1111-1111-1111-111111111111"
    nav_feed.reset_nav_feed()
    monkeypatch.setenv("SIGNALPOST_NAV_FEED_TOKEN", "a.b.c")
    monkeypatch.setattr(nav_feed, "_index", {"fjord teknologi": [{"uuid": "11111111-1111-1111-1111-111111111111", "name": company, "url": url}]})
    monkeypatch.setattr(nav_feed, "_token", "a.b.c")
    monkeypatch.setattr(nav_feed, "_state", "complete")

    def detail(status: str, employer_org: str) -> bytes:
        return json.dumps({
            "uuid": "11111111-1111-1111-1111-111111111111",
            "status": status,
            "ad_content": {"title": "Engineer", "published": "2026-11-01", "expires": "2099-01-01",
                           "employer": {"name": company, "orgnr": employer_org, "homepage": "https://fjord.example"}},
        }).encode()

    monkeypatch.setattr(nav_feed, "_get", lambda *args, **kwargs: (detail("ACTIVE", "999999999"), {}))
    jobs, homes, _ = nav_feed.nav_feed_jobs_for_company(company, "111111111", budget=_Budget())
    assert jobs == [] and homes == []

    monkeypatch.setattr(nav_feed, "_get", lambda *args, **kwargs: (detail("INACTIVE", "111111111"), {}))
    jobs, homes, _ = nav_feed.nav_feed_jobs_for_company(company, "111111111", budget=_Budget())
    assert jobs == [] and homes == []

    monkeypatch.setattr(nav_feed, "_get", lambda *args, **kwargs: (detail("ACTIVE", "111111111"), {}))
    jobs, homes, _ = nav_feed.nav_feed_jobs_for_company(company, "111111111", budget=_Budget())
    assert len(jobs) == 1 and jobs[0]["employer_org"] == "111111111"
    assert homes == ["https://fjord.example"]
    vacancy = jobs[0]
    assert vacancy["valid_through"] == "2099-01-01"
    assert observation_decision(vacancy).publishable
    claim = {**vacancy, "title": vacancy["job_title"], "url": vacancy["source_url"]}
    assert validate_job_item(claim, org="111111111", company_name=company)[0]
    assert not validate_job_item({**claim, "valid_through": "2000-01-01"}, org="111111111", company_name=company)[0]
    assert not observation_decision({**vacancy, "nav_feed": {**vacancy["nav_feed"], "registered_access": False}}).publishable
    saved = {**vacancy, "nav_feed": {**vacancy["nav_feed"], "checked_at": "2025-02-02T10:00:00Z"}}
    assert not observation_decision(saved).publishable
    assert observation_decision(saved, snapshot_time=datetime(2025, 2, 2, 10, 5, tzinfo=timezone.utc)).publishable
    nav_feed.reset_nav_feed()


def test_official_run_does_not_fetch_the_public_experiment_token(monkeypatch) -> None:
    monkeypatch.delenv("SIGNALPOST_NAV_FEED_TOKEN", raising=False)
    monkeypatch.setattr(nav_feed, "_build_index", lambda *_: None)
    jobs, homes, diagnostics = nav_feed.nav_feed_jobs_for_company("Example AS", "111111111", budget=_Budget())
    assert jobs == homes == []
    assert diagnostics["feed_state"] == "registered_token_required"


def test_feed_urls_cannot_leave_nav_host() -> None:
    assert nav_feed._nav_url("/api/v1/feedentry/abc", "/api/v1/feedentry/")
    assert nav_feed._nav_url("https://other.example/api/v1/feedentry/abc", "/api/v1/feedentry/") is None
    assert nav_feed._nav_url("/internal", "/api/v1/feedentry/") is None


def test_registered_feed_resumes_checkpoint_and_removes_inactive_ad(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "nav.json"
    cursor = "/api/v1/feed/terminal"
    uuid = "11111111-1111-1111-1111-111111111111"
    detail_url = f"/api/v1/feedentry/{uuid}"
    requests = []

    def page(url, budget, *, token, since):
        requests.append((url, since))
        if len(requests) == 1:
            return json.dumps({"next_url": cursor, "items": []}).encode(), {}
        if len(requests) == 2:
            entry = {"uuid": uuid, "status": "ACTIVE", "businessName": "Example AS"}
        else:
            entry = {"uuid": uuid, "status": "INACTIVE"}
        return json.dumps({"next_url": None, "items": [{"url": detail_url, "_feed_entry": entry}]}).encode(), {}

    monkeypatch.setattr(nav_feed, "_get", page)
    before, _, _ = nav_feed._build_index(_Budget(), "a.b.c", cache)
    assert before["example"][0]["uuid"] == uuid
    after, _, _ = nav_feed._build_index(_Budget(), "a.b.c", cache)
    assert after == {}
    assert requests[0][1]
    assert requests[-1] == (nav_feed.BASE + cursor, "")
    assert "a.b.c" not in cache.read_text()


def test_registered_nav_keeps_exact_employer_lookup_and_expiry(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "nav.json"
    org = "964118191"
    uuid = "11111111-1111-1111-1111-111111111111"
    detail_url = nav_feed.BASE + "/api/v1/feedentry/" + uuid
    original = {"uuid": uuid, "url": detail_url, "name": "Old employer display name", "revision": "1"}
    nav_feed.reset_nav_feed()
    monkeypatch.setenv("SIGNALPOST_NAV_FEED_TOKEN", "a.b.c")
    nav_feed._save_index(cache, nav_feed.FEED_URL, {uuid: original})
    monkeypatch.setattr(nav_feed, "_index", {"old employer display name": [original]})
    monkeypatch.setattr(nav_feed, "_token", "a.b.c")
    monkeypatch.setattr(nav_feed, "_state", "complete")
    detail = {"uuid": uuid, "status": "ACTIVE", "ad_content": {
        "title": "Service Engineer", "employer": {"orgnr": org}, "expires": "2099-01-01"}}
    monkeypatch.setattr(nav_feed, "_get", lambda *_args, **_kw: (json.dumps(detail).encode(), {}))
    jobs, _, _ = nav_feed.nav_feed_jobs_for_company("Old employer display name", org, budget=_Budget(), cache_path=cache)
    assert len(jobs) == 1
    nav_feed.flush_nav_feed_details(cache)
    _, rows = nav_feed._load_index(cache)
    assert rows[uuid]["employer_org"] == org and rows[uuid]["expires"] == "2099-01-01"
    monkeypatch.setattr(nav_feed, "_get", lambda *_args, **_kw: (b'{"items":[],"next_url":null}', {}))
    refreshed, _, _ = nav_feed._build_index(_Budget(), "a.b.c", cache)
    assert refreshed[f"org:{org}"][0]["uuid"] == uuid
    nav_feed.reset_nav_feed()
