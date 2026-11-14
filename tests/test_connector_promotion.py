from norway_company_agent.promotion import evaluate_promotion, promotion_report


def reviewed(org: str, **changes):
    row = {
        "strategy": "candidate_connector_v1",
        "organisation_number": org,
        "reviewed": True,
        "exact_entity": True,
        "evidence_supported": True,
        "evidence_complete": True,
        "new_supported_claim": True,
    }
    row.update(changes)
    return row


def test_promotes_only_reviewed_safe_coverage_gain():
    report = evaluate_promotion("candidate_connector_v1", [reviewed("900000001")])
    assert report.promoted
    assert report.coverage_gain == 1


def test_rejects_exact_name_collision():
    report = evaluate_promotion("candidate_connector_v1", [reviewed("900000001", exact_entity=False)])
    assert not report.promoted
    assert report.wrong_company == 1


def test_rejects_parent_or_subsidiary_collapse():
    report = evaluate_promotion(
        "candidate_connector_v1",
        [reviewed("900000001", exact_entity=False, relationship="parent_not_target")],
    )
    assert not report.promoted
    assert "wrong-company publication detected" in report.reasons


def test_rejects_unreviewed_connector_assertions():
    report = evaluate_promotion("candidate_connector_v1", [reviewed("900000001", reviewed=False)])
    assert not report.promoted
    assert report.reviewed == 0


def test_full_report_requires_safe_declaration_and_budget():
    report = promotion_report(
        "company_site_job",
        [reviewed("900000001", strategy="company_site_job")],
        {"requests": 20, "runtime_seconds": 3, "third_party_cost_usd": 0},
    )
    assert report["promoted"]
    assert all(question["passed"] for question in report["questions"].values())


def test_full_report_rejects_budget_overrun():
    report = promotion_report(
        "company_site_job",
        [reviewed("900000001", strategy="company_site_job")],
        {"requests": 2_001, "runtime_seconds": 3, "third_party_cost_usd": 0},
    )
    assert not report["promoted"]
    assert not report["questions"]["within_request_runtime_cost_budget"]["passed"]


def test_unlicensed_website_job_search_is_excluded_from_official_promotion():
    report = promotion_report(
        "nav_arbeidsplassen_search",
        [reviewed("900000001", strategy="nav_arbeidsplassen_search")],
        {"requests": 20, "runtime_seconds": 3, "third_party_cost_usd": 0},
    )
    assert not report["promoted"]
