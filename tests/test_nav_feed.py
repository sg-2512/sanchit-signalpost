"""NAV feed candidates never become claims without a live exact-employer detail."""
from __future__ import annotations

import json

from norway_company_agent.connectors import nav_feed


class _Budget:
    def can_proceed(self, **_: object) -> bool:
        return True


def test_live_detail_rejects_other_employer_and_inactive(monkeypatch) -> None:
    company = "FJORD TEKNOLOGI AS"
    url = "https://pam-stilling-feed.nav.no/api/v1/feedentry/11111111-1111-1111-1111-111111111111"
    nav_feed.reset_nav_feed()
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
    nav_feed.reset_nav_feed()


def test_feed_urls_cannot_leave_nav_host() -> None:
    assert nav_feed._nav_url("/api/v1/feedentry/abc", "/api/v1/feedentry/")
    assert nav_feed._nav_url("https://other.example/api/v1/feedentry/abc", "/api/v1/feedentry/") is None
    assert nav_feed._nav_url("/internal", "/api/v1/feedentry/") is None
