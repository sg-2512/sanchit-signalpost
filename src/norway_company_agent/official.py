from __future__ import annotations

import math
import threading
import time
from datetime import date
from typing import Any, Callable

from .evidence import evidence
from .http import FetchResult, fetch_json

ACCOUNTING_OBLIGATION_SOURCE = "https://www.brreg.no/en/submission-of-annual-accounts/reporting-obligations-to-the-register-of-company-accounts/who-has-an-accounting-obligation/"
ACCOUNTING_RULESET_VERSION = "brreg_accounting_obligation_rules_2024-08-09_v1"
ALWAYS_ACCOUNTING_OBLIGED_FORMS = {"AS", "ASA", "BRL", "BBL", "STI", "SF", "VPFO"}
THRESHOLD_OR_ACTIVITY_FORMS = {"ENK", "ANS", "DA", "SA", "FLI", "ESEK", "NUF", "UTLA", "ORGL", "SAM", "SPA", "KS", "BO"}

BRREG_ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
BRREG_ROLES = BRREG_ENTITY + "/roller"
BRREG_GROUP = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
BRREG_SUBUNITS = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
BRREG_ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
BRREG_ACCOUNT_YEARS = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
BRREG_ACCOUNT_PDF = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"

_history_lock = threading.Lock()
_history_last_request = 0.0


def accounting_obligation_assessment(
    profile: dict[str, Any],
    content_sha256: str = "",
    source_url: str = "",
) -> dict[str, Any]:
    """Classify the rule path without inventing a definitive exemption.

    Brreg's public rule is categorical for some forms and threshold/activity dependent for others.
    Registry employee counts are not treated as equivalent to statutory man-years or asset/revenue tests.
    """
    org_form_obj = profile.get("organisasjonsform")
    raw_form = org_form_obj.get("kode") if isinstance(org_form_obj, dict) else profile.get("organisasjonsform.kode")
    legal_form = str(profile.get("legal_form") or raw_form or "").upper()
    latest = profile.get("latest_submitted_accounts") or profile.get("sisteInnsendteAarsregnskap")
    if latest:
        classification = "filing_observed"
        reason = "The registry snapshot reports a latest submitted annual-account year."
    elif legal_form in ALWAYS_ACCOUNTING_OBLIGED_FORMS:
        classification = "required_by_legal_form"
        reason = f"Brreg lists organisation form {legal_form} in an always-obliged category."
    elif legal_form in THRESHOLD_OR_ACTIVITY_FORMS:
        classification = "threshold_or_activity_dependent"
        reason = "Obligation depends on statutory size, partner, activity, tax or supervision tests not fully present in the open entity row."
    else:
        classification = "special_rule_or_review_required"
        reason = "The open entity row is insufficient for a definitive accounting-obligation decision."
    ev_dict = profile.get("evidence") if isinstance(profile.get("evidence"), dict) else {}
    reg_ev = (ev_dict or {}).get("registry_live") or (ev_dict or {}).get("registry") or {}
    ruleset_hash = (
        content_sha256
        or reg_ev.get("content_sha256")
        or __import__("hashlib").sha256(ACCOUNTING_RULESET_VERSION.encode()).hexdigest()
    )
    resolved_url = source_url or reg_ev.get("source_url") or ACCOUNTING_OBLIGATION_SOURCE
    org_num = str(profile.get("organisation_number") or profile.get("organisasjonsnummer") or "") or None
    return evidence(
        "accounting_obligation",
        "available",
        "official_rule_interpretation",
        resolved_url,
        value={
            "classification": classification,
            "legal_form": legal_form or None,
            "latest_submitted_accounts": latest or None,
            "reason": reason,
            "ruleset_version": ACCOUNTING_RULESET_VERSION,
        },
        as_of="2024-08-09",
        content_sha256=ruleset_hash,
        source_row_key=org_num,
    )


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _period_end(item: Any) -> str:
    period = item.get("regnskapsperiode") if isinstance(item, dict) else None
    return str((period or {}).get("tilDato") or "") if isinstance(period, dict) else ""


def select_annual_records(body: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Return one record per fiscal year, newest first.

    Regnskapsregisteret lists accounts oldest-first, so callers must never rely on list order.
    Records without a period end date are dropped. Per year the company's own accounts
    (SELSKAP) win over group accounts (KONSERN), then the highest record id.
    """
    best: dict[str, dict[str, Any]] = {}
    for item in body if isinstance(body, list) else []:
        end = _period_end(item)
        if not isinstance(item, dict) or len(end) < 4 or not end[:4].isdigit():
            continue
        year = end[:4]
        rank = (1 if str(item.get("regnskapstype") or "").upper() == "SELSKAP" else 0, int(item.get("id") or 0))
        current = best.get(year)
        if current is None or rank > (
            1 if str(current.get("regnskapstype") or "").upper() == "SELSKAP" else 0,
            int(current.get("id") or 0),
        ):
            best[year] = item
    ordered = sorted(best.values(), key=_period_end, reverse=True)
    return ordered[:limit]


def latest_account(records: Any) -> dict[str, Any]:
    """Newest fiscal-year record from normalised records (any input order)."""
    rows = [r for r in (records or []) if isinstance(r, dict)]
    if not rows:
        return {}

    def _key(row: dict[str, Any]) -> str:
        period = row.get("period")
        end = str((period or {}).get("tilDato") or "") if isinstance(period, dict) else ""
        return end or str(row.get("filing_year") or "")

    return max(rows, key=_key)


def fiscal_year_of(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    period = record.get("period")
    end = str((period or {}).get("tilDato") or "") if isinstance(period, dict) else ""
    if len(end) >= 4 and end[:4].isdigit():
        return end[:4]
    year = str(record.get("filing_year") or "")
    return year if year.isdigit() else None


def validate_balance_sheet(record: dict[str, Any]) -> None:
    """Withhold invalid aggregates and ratios while preserving source evidence."""
    issues = dict(record.get("balance_sheet_issues") or {})
    for field in ("current_assets", "assets", "debt", "short_term_debt"):
        amount = record.get(field)
        if amount is not None and (
            not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or amount < 0
        ):
            issues[field] = "invalid_nonnegative_aggregate"
            record[field] = None
    assets, equity, debt = (record.get(field) for field in ("assets", "equity", "debt"))
    current_assets, short_debt = (record.get(field) for field in ("current_assets", "short_term_debt"))
    if all(isinstance(amount, (int, float)) for amount in (assets, equity, debt)):
        if not math.isclose(assets, equity + debt, rel_tol=1e-6, abs_tol=2):
            issues["balance_sheet"] = "assets_do_not_reconcile_with_equity_and_debt"
    for field, part, total in (("current_assets", current_assets, assets), ("short_term_debt", short_debt, debt)):
        if part is not None and total is not None and part > total and not math.isclose(part, total, rel_tol=1e-6, abs_tol=2):
            issues[field] = "aggregate_exceeds_total"
            record[field] = None
    if issues:
        record["balance_sheet_issues"] = issues
    if "current_assets" in issues or "short_term_debt" in issues or "balance_sheet" in issues:
        record.update(liquidity_ratio=None, current_ratio=None)
    if "balance_sheet" in issues or "assets" in issues:
        record.update(equity_ratio=None, solvency_status=None, debt_to_equity=None, derived_assessment={})
    if "debt" in issues:
        record["debt_to_equity"] = None


def account_period_and_scope(record: dict[str, Any]) -> dict[str, Any]:
    scope = {"SELSKAP": "standalone_statutory", "KONSERN": "consolidated_group"}.get(record.get("account_type"), "unspecified")
    period = record.get("period") or {}
    try:
        days = (date.fromisoformat(period["tilDato"]) - date.fromisoformat(period["fraDato"])).days + 1
    except (KeyError, TypeError, ValueError):
        days = 0
    return {"account_scope": scope, "period_days": days if days > 0 else None,
            "unusual_fiscal_period": bool(days > 0 and not 350 <= days <= 380)}


def cash_and_bank_deposits(assets: dict[str, Any]) -> Any:
    total = assets.get("sumBankinnskuddOgKontanter")
    return total if total is not None else _get(assets, "omloepsmidler", "bankinnskuddKontanterOgLignende")


def normalize_retained_accounts(financials: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve saved filing values against saved structured source fields."""
    value = financials.get("value") or {}
    records = value.get("records") or []
    source_fields = {field["source_pointer"]: field["source_value"]
                     for field in financials.get("source_fields") or []
                     if isinstance(field, dict) and "source_pointer" in field and "source_value" in field}
    assets_by_record = {
        source_fields[pointer.removesuffix("/eiendeler") + "/id"]: assets
        for pointer, assets in source_fields.items()
        if pointer.endswith("/eiendeler") and isinstance(assets, dict)
        and source_fields.get(pointer.removesuffix("/eiendeler") + "/id") is not None
    }
    for record in records:
        assets = assets_by_record.get(record.get("record_id"))
        if assets is not None and (cash := cash_and_bank_deposits(assets)) is not None:
            record["cash_and_bank_deposits"] = cash
        validate_balance_sheet(record)
        record.update(account_period_and_scope(record))
    if records:
        latest = latest_account(records)
        value["latest_fiscal_year"] = fiscal_year_of(latest)
        trends = value.setdefault("financial_trends", {})
        if latest.get("cash_and_bank_deposits") is not None:
            trends["cash_and_bank_deposits"] = latest["cash_and_bank_deposits"]
    return records


def compare_annual_accounts(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = latest_account(records)
    year = fiscal_year_of(latest)
    if not year or not latest.get("period_days") or latest.get("unusual_fiscal_period") or not latest.get("currency"):
        return None
    previous = next((record for record in records if fiscal_year_of(record) == str(int(year) - 1)
                     and record.get("account_type") == latest.get("account_type")
                     and record.get("currency") == latest.get("currency") and record.get("period_days")
                     and not record.get("unusual_fiscal_period")), None)
    if not previous or not latest.get("account_type"):
        return None
    changes = {}
    for field in ("revenue", "operating_result", "annual_result", "assets", "equity", "debt"):
        before, after = previous.get(field), latest.get(field)
        if all(isinstance(amount, (int, float)) and math.isfinite(amount) for amount in (before, after)) and before != after:
            changes[field] = {"previous": before, "current": after, "change": after - before,
                              "change_pct": round((after - before) / before * 100, 2) if before > 0 else None}
    return {"previous_year": fiscal_year_of(previous), "current_year": year, "currency": latest["currency"],
            "previous_period": previous.get("period"), "current_period": latest.get("period"),
            "account_scope": latest.get("account_scope"), "changes": changes} if changes else None


def normalize_financials(body: Any, employee_count: int | None = None) -> dict[str, Any]:
    """Normalize statutory amounts. The legacy headcount argument is accepted for callers."""
    records = select_annual_records(body)
    if not records:
        return {"records": [], "financial_trends": {}}
    normalized = []
    for item in records:
        rev = _get(item, "resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter")
        op_res = _get(item, "resultatregnskapResultat", "driftsresultat", "driftsresultat")
        op_costs = _get(item, "resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad")
        wages = _get(item, "resultatregnskapResultat", "driftsresultat", "driftskostnad", "loennskostnad")
        assets = _get(item, "eiendeler", "sumEiendeler")
        equity = _get(item, "egenkapitalGjeld", "egenkapital", "sumEgenkapital")
        retained_earnings = _get(item, "egenkapitalGjeld", "egenkapital", "opptjentEgenkapital", "sumOpptjentEgenkapital")
        cur_assets = _get(item, "eiendeler", "omloepsmidler", "sumOmloepsmidler")
        cash_deposits = cash_and_bank_deposits(item.get("eiendeler") or {})
        st_debt = _get(item, "egenkapitalGjeld", "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld")
        total_debt = _get(item, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld")
        balance = {"assets": assets, "equity": equity, "debt": total_debt,
                   "current_assets": cur_assets, "short_term_debt": st_debt}
        validate_balance_sheet(balance)
        assets, total_debt, cur_assets, st_debt = (balance[key] for key in ("assets", "debt", "current_assets", "short_term_debt"))
        reconciled = "balance_sheet" not in (balance.get("balance_sheet_issues") or {})

        # Institutional diligence metrics
        op_margin = round(float(op_res) / float(rev), 4) if rev and op_res is not None and float(rev) > 0 else None
        eq_ratio = round(float(equity) / assets, 4) if reconciled and assets and equity is not None else None
        liq_ratio = round(cur_assets / st_debt, 4) if reconciled and cur_assets is not None and st_debt else None
        debt_to_equity = round(total_debt / float(equity), 4) if reconciled and total_debt is not None and equity and float(equity) > 0 else None
        
        solvency = None
        if eq_ratio is not None:
            if eq_ratio >= 0.40:
                solvency = "Strong"
            elif eq_ratio >= 0.20:
                solvency = "Adequate"
            else:
                solvency = "Vulnerable"

        normalized.append({
            **balance,
            "record_id": item.get("id"),
            "account_type": item.get("regnskapstype"),
            "period": item.get("regnskapsperiode"),
            "currency": item.get("valuta"),
            "revenue": rev,
            "operating_costs": op_costs,
            "operating_result": op_res,
            "wage_expenses": wages,
            "profit_before_tax": _get(item, "resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
            "annual_result": _get(item, "resultatregnskapResultat", "aarsresultat"),
            "retained_earnings": retained_earnings,
            "debt_to_equity": debt_to_equity,
            "cash_and_bank_deposits": cash_deposits,
            "operating_margin": op_margin,
            "equity_ratio": eq_ratio,
            "liquidity_ratio": liq_ratio,
            "current_ratio": liq_ratio,
            "solvency_status": solvency,
            "derived_assessment": {
                "solvency": {
                    "value": solvency,
                    "method": "equity_ratio_thresholds_v1",
                    "formula": "equity / assets",
                    "thresholds": {"strong_at_or_above": 0.40, "adequate_at_or_above": 0.20},
                },
            } if solvency else {},
        })
        normalized[-1].update(account_period_and_scope(normalized[-1]))

    # Multi-year trajectory & CAGR computation
    trends: dict[str, Any] = {}
    comparable = all(record["account_scope"] == normalized[0]["account_scope"]
                     and record["currency"] == normalized[0]["currency"] and not record["unusual_fiscal_period"]
                     for record in normalized[:3])
    if comparable and len(normalized) >= 2:
        rev_0 = normalized[0].get("revenue")
        rev_1 = normalized[1].get("revenue")
        if rev_0 is not None and rev_1 is not None and float(rev_1) > 0:
            growth = round((float(rev_0) - float(rev_1)) / float(rev_1), 4)
            trends["yoy_revenue_growth"] = growth
            trends["growth_label"] = f"{'+' if growth >= 0 else ''}{growth*100:.1f}%"

    if comparable and len(normalized) >= 3:
        rev_0 = normalized[0].get("revenue")
        rev_2 = normalized[2].get("revenue")
        if rev_0 is not None and rev_2 is not None and float(rev_2) > 0 and float(rev_0) > 0:
            cagr = round((float(rev_0) / float(rev_2)) ** 0.5 - 1.0, 4)
            trends["cagr_3yr"] = cagr
            trends["cagr_label"] = f"{'+' if cagr >= 0 else ''}{cagr*100:.1f}%"

    if normalized and normalized[0].get("solvency_status"):
        latest = normalized[0]
        trends["solvency_status"] = latest["solvency_status"]
        trends["equity_ratio"] = latest["equity_ratio"]
        if latest.get("operating_margin") is not None:
            trends["operating_margin"] = latest["operating_margin"]
        if latest.get("liquidity_ratio") is not None:
            trends["liquidity_ratio"] = latest["liquidity_ratio"]
            trends["current_ratio"] = latest["liquidity_ratio"]
        if latest.get("debt_to_equity") is not None:
            trends["debt_to_equity"] = latest["debt_to_equity"]
        if latest.get("cash_and_bank_deposits") is not None:
            trends["cash_and_bank_deposits"] = latest["cash_and_bank_deposits"]
        if latest.get("operating_costs") is not None:
            trends["operating_costs"] = latest["operating_costs"]
        if latest.get("retained_earnings") is not None:
            trends["retained_earnings"] = latest["retained_earnings"]

    return {
        "records": normalized,
        "financial_trends": trends,
        "latest_fiscal_year": fiscal_year_of(normalized[0]),
    }


def normalize_financial_history(body: Any, org: str) -> dict[str, Any]:
    years = sorted({str(year) for year in body if str(year).isdigit()}) if isinstance(body, list) else []
    return {
        "years": years,
        "pdfs": [
            {"year": year, "url": BRREG_ACCOUNT_PDF.format(org=org, year=year)}
            for year in reversed(years)
        ],
    }


def _reserve_history_slot(clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
    """Reserve starts at least 2.1 seconds apart without serializing response time."""
    global _history_last_request
    with _history_lock:
        delay = 2.1 - (clock() - _history_last_request)
        if delay > 0:
            sleeper(delay)
        _history_last_request = clock()


def _fetch_history(url: str) -> FetchResult:
    """Keep this endpoint below its observed 30-request-starts/minute allowance."""
    _reserve_history_slot()
    return fetch_json(url)


def normalize_roles(body: Any) -> dict[str, Any]:
    roles = []
    chair = None
    ceo = None
    board_members = []
    for group in body.get("rollegrupper", []) if isinstance(body, dict) else []:
        changed = group.get("sistEndret")
        for item in group.get("roller", []):
            person = item.get("person") or {}
            name = person.get("navn") or {}
            entity = item.get("enhet") or {}
            display_name = " ".join(filter(None, [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")])) or entity.get("navn")
            r_code = _get(item, "type", "kode")
            r_desc = _get(item, "type", "beskrivelse")
            inactive = bool(item.get("avregistrert"))
            role_dict = {
                "name": display_name or None,
                "organisation_number": entity.get("organisasjonsnummer"),
                "role_code": r_code,
                "role": r_desc,
                "group_code": _get(group, "type", "kode"),
                "group": _get(group, "type", "beskrivelse"),
                "last_changed": changed,
                "inactive": inactive,
            }
            roles.append(role_dict)
            if not inactive and display_name:
                role_code = str(r_code or "").upper()
                role_description = str(r_desc or "").strip().casefold()
                if (role_code == "LEDE" or (not role_code and role_description in {"styrets leder", "styreleder", "chair"})) and not chair:
                    chair = display_name
                elif (role_code == "DAGL" or (not role_code and role_description in {"daglig leder", "administrerende direktør", "ceo", "managing director"})) and not ceo:
                    ceo = display_name
                elif role_code in {"MEDL", "NEST"} or (not role_code and role_description in {"styremedlem", "nestleder"}):
                    board_members.append(display_name)

    return {
        "roles": roles,
        "chair": chair,
        "ceo": ceo,
        "board_members": sorted(set(board_members)),
    }


def normalize_group(body: Any, org: str | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"parent": None, "subsidiaries": [], "subsidiary_count": 0}
    if isinstance(body.get("children"), list) and org:
        from .evidence import capture_source_fields

        def find_target(node, path="", parent=None):
            if node.get("organisasjonsnummer") == org:
                return node, path, parent
            for index, child in enumerate(node.get("children") or []):
                if isinstance(child, dict):
                    found = find_target(child, f"{path}/children/{index}", node)
                    if found:
                        return found
            return None

        found = find_target(body)
        if not found:
            return {"parent": None, "subsidiaries": [], "subsidiary_count": 0}
        target, path, parent_node = found
        parent = None
        paths = [f"{path}/organisasjonsnummer"]
        if parent_node and target.get("parentOrganisasjonsnummer") == parent_node.get("organisasjonsnummer"):
            parent = {"organisation_number": target["parentOrganisasjonsnummer"], "name": target.get("parentNavn"),
                      "navn": target.get("parentNavn"), "basis": target.get("grunnlag")}
            paths.extend(f"{path}/{field}" for field in ("parentOrganisasjonsnummer", "parentNavn", "grunnlag"))
        subsidiaries = []
        for index, child in enumerate(target.get("children") or []):
            if not isinstance(child, dict) or child.get("parentOrganisasjonsnummer") != org or not child.get("organisasjonsnummer"):
                continue
            basis = child.get("grunnlag")
            percent = str(basis or "").strip().removesuffix("%").replace(",", ".")
            subsidiaries.append({"organisation_number": child["organisasjonsnummer"], "name": child.get("navn"),
                                 "basis": basis, "ownership_percentage": float(percent) if str(basis).endswith("%") and percent.replace(".", "", 1).isdigit() else None})
            paths.extend(f"{path}/children/{index}/{field}" for field in
                         ("organisasjonsnummer", "navn", "parentOrganisasjonsnummer", "grunnlag"))
        return {"organisation_number": org, "parent": parent, "subsidiaries": subsidiaries,
                "subsidiary_count": len(subsidiaries), "source_fields": capture_source_fields(body, paths)}
    parent = body.get("morselskap") or body.get("parent")
    subsidiaries = body.get("datterselskaper") or body.get("subsidiaries") or body.get("underenheter") or []
    normalized_subs = []
    for s in subsidiaries if isinstance(subsidiaries, list) else []:
        if isinstance(s, dict):
            normalized_subs.append({
                "organisation_number": s.get("organisasjonsnummer"),
                "name": s.get("navn"),
                "ownership_percentage": s.get("eierandel") if s.get("eierandel") is not None else s.get("andel"),
            })
    return {
        "parent": {
            "organisation_number": parent.get("organisasjonsnummer"),
            "name": parent.get("navn") or parent.get("name"),
            "navn": parent.get("navn") or parent.get("name"),
        } if isinstance(parent, dict) else None,
        "subsidiaries": normalized_subs,
        "subsidiary_count": len(normalized_subs),
    }


def normalize_locations(body: Any) -> dict[str, Any]:
    rows = _get(body, "_embedded", "underenheter") or []
    return {"locations": [{
        "organisation_number": item.get("organisasjonsnummer"),
        "name": item.get("navn"),
        "address": item.get("beliggenhetsadresse") or item.get("postadresse"),
        "industry": item.get("naeringskode1"),
        "employees": item.get("antallAnsatte"),
        **({"website": item["hjemmeside"]} if isinstance(item.get("hjemmeside"), str) and item["hjemmeside"].strip() else {}),
    } for item in rows]}


def normalize_entity(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    return {
        **body,
        "organisation_number": body.get("organisasjonsnummer"),
        "name": body.get("navn"),
        "legal_form": _get(body, "organisasjonsform", "kode"),
        "employees": body.get("antallAnsatte"),
        "bankrupt": body.get("konkurs"),
        "liquidating": body.get("underAvvikling"),
        "website": body.get("hjemmeside"),
        "epostadresse": body.get("epostadresse"),
        "telefon": body.get("telefon"),
        "industry": body.get("naeringskode1"),
        "business_address": body.get("forretningsadresse"),
        "postal_address": body.get("postadresse"),
        "latest_submitted_accounts": body.get("sisteInnsendteAarsregnskap"),
    }


def _classified(field: str, source_type: str, result: FetchResult, value: Any = None) -> dict[str, Any]:
    if result.status == 200:
        from .evidence import capture_source_fields

        record = evidence(field, "available", source_type, result.url, value=value, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at, http_status=result.status)
        paths = {
            "registry_live": ("/organisasjonsnummer", "/navn", "/organisasjonsform/kode", "/forretningsadresse",
                              "/antallAnsatte", "/hjemmeside", "/sisteInnsendteAarsregnskap", "/naeringskode1", "/stiftelsesdato",
                              "/registreringsdatoEnhetsregisteret", "/registreringsdatoForetaksregisteret"),
        }.get(field, ())
        if field == "group" and isinstance(value, dict) and value.get("source_fields"):
            record["source_fields"] = value.pop("source_fields")
        if field == "roles" and isinstance(result.body, dict):
            paths = tuple(
                f"/rollegrupper/{group_index}/roller/{role_index}/{field_path}"
                for group_index, group in enumerate(result.body.get("rollegrupper") or [])
                for role_index, _ in enumerate(group.get("roller") or [])
                for field_path in ("type/kode", "type/beskrivelse", "person/navn", "enhet/navn",
                                   "enhet/organisasjonsnummer", "avregistrert")
            )
        if field == "locations" and isinstance(result.body, dict):
            paths = tuple(
                f"/_embedded/underenheter/{index}/{field_path}"
                for index, _ in enumerate(_get(result.body, "_embedded", "underenheter") or [])
                for field_path in ("organisasjonsnummer", "overordnetEnhet", "navn", "beliggenhetsadresse", "postadresse",
                                   "naeringskode1", "antallAnsatte", "hjemmeside")
            )
        if field == "financials" and isinstance(result.body, list):
            selected = {id(item) for item in select_annual_records(result.body)}
            paths = tuple(f"/{index}{path}" for index, item in enumerate(result.body) if id(item) in selected
                          for path in ("/id", "/regnskapsperiode", "/regnskapstype", "/valuta",
                                       "/resultatregnskapResultat", "/eiendeler", "/egenkapitalGjeld"))
        if paths:
            record["source_fields"] = capture_source_fields(result.body, paths)
        return record
    if result.status in {404, 410}:
        return evidence(field, "not_found", source_type, result.url, note=result.error, retrieved_at=result.retrieved_at, effective_at=result.effective_at, http_status=result.status)
    if result.status in {401, 403, 429}:
        return evidence(field, "blocked", source_type, result.url, note=result.error, retrieved_at=result.retrieved_at, effective_at=result.effective_at, http_status=result.status)
    return evidence(field, "source_error", source_type, result.url, note=result.error, retrieved_at=result.retrieved_at, effective_at=result.effective_at, http_status=result.status or None)


def fetch_official_modules(org: str, modules: set[str], fetcher: Callable[[str], FetchResult] = fetch_json) -> tuple[dict[str, Any], list[FetchResult]]:
    records: dict[str, Any] = {}
    metrics: list[FetchResult] = []
    endpoints = {
        "registry_live": (BRREG_ENTITY.format(org=org), "official_registry_live"),
        "financials": (BRREG_ACCOUNTS.format(org=org), "official_annual_accounts"),
        "financial_history": (BRREG_ACCOUNT_YEARS.format(org=org), "official_annual_account_copies"),
        "roles": (BRREG_ROLES.format(org=org), "official_roles"),
        "group": (BRREG_GROUP.format(org=org), "official_group_structure"),
        "locations": (BRREG_SUBUNITS.format(org=org), "official_subunits"),
    }
    for module, (url, source_type) in endpoints.items():
        if module not in modules:
            continue
        result = _fetch_history(url) if module == "financial_history" and fetcher is fetch_json else fetcher(url)
        if result.error != "budget_exhausted" and (result.status == 0 or 500 <= result.status <= 504):
            # Transient gateway/network glitch: 1 attempt jittered retry with backoff
            time.sleep(1.0)
            retry_res = fetcher(url)
            if retry_res.status == 200 or retry_res.status in {404, 410}:
                result = retry_res
        metrics.append(result)
        if result.status == 200:
            # Successful fetch: normalize records normally
            normalized = (
                normalize_entity(result.body) if module == "registry_live"
                else normalize_financials(result.body) if module == "financials"
                else normalize_financial_history(result.body, org) if module == "financial_history"
                else normalize_roles(result.body) if module == "roles"
                else normalize_group(result.body, org) if module == "group"
                else normalize_locations(result.body) if module == "locations"
                else result.body
            )
            records[module] = _classified(module, source_type, result, value=normalized)
            # Record that we successfully fetched financial data
            if module == "financials" and isinstance(normalized, dict) and normalized.get("records"):
                latest_fy = fiscal_year_of(normalized["records"][0])
                if latest_fy and "registry_live" in records:
                    records["registry_live"].setdefault("value", {})["latest_financial_year"] = latest_fy
        else:
            # HTTP 503 or network failure: fallback to statutory filing from registry_live if available
            # An unavailable endpoint is not evidence that the company has no
            # group, subunits or financial statements. Filing year remains in
            # registry_live; financial figures require their own response.
            records[module] = _classified(module, source_type, result, value=None)
    return records, metrics
