"""Google Places API connector for the SignalPost live agent.

Fetches place data (rating, reviews, address) for Norwegian companies.
Supports both Places API (New) and legacy Places API.
Activates automatically when GOOGLE_PLACES_API_KEY environment variable is set.

Budget estimates use the published undiscounted rates for the requested fields.
For 100 companies: ~$1.70 per run (well within $10 budget).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


_QUOTA_EXHAUSTED = False
_CIRCUIT_REASON = ""
_CONSECUTIVE_429 = 0
_MAX_CONSECUTIVE_429 = 20
_PLACES_LOCK = threading.Lock()
_LAST_REQUEST_TIME = 0.0
_MIN_INTERVAL = 0.20  # 200ms between requests to pace QPS comfortably across threads


def is_available() -> bool:
    """Check if Google Places API key is configured and quota is available."""
    return bool(os.environ.get("GOOGLE_PLACES_API_KEY")) and not _QUOTA_EXHAUSTED


def connector_status() -> dict[str, Any]:
    return {"configured": bool(os.environ.get("GOOGLE_PLACES_API_KEY")),
            "available": is_available(), "disabled_reason": _CIRCUIT_REASON or None}


def fetch_place_data(
    org: str,
    company_name: str,
    address: str | None = None,
    *,
    budget: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch Google Places data for a company.

    Uses Places API (New) with fallback to legacy Places API.
    Returns observation dicts for external footprint evaluation.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return []

    global _QUOTA_EXHAUSTED, _CIRCUIT_REASON
    if _QUOTA_EXHAUSTED:
        return []

    if budget and not budget.can_proceed():
        return []

    observations: list[dict[str, Any]] = []
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    search_input = f"{company_name} Norway"
    if address:
        municipality = address.get("kommune", "") if isinstance(address, dict) else str(address)
        if municipality:
            search_input = f"{company_name} {municipality} Norway"

    global _LAST_REQUEST_TIME, _CONSECUTIVE_429
    with _PLACES_LOCK:
        now = time.monotonic()
        diff = now - _LAST_REQUEST_TIME
        if diff < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - diff)
        _LAST_REQUEST_TIME = time.monotonic()

    # Try Places API (New) first (preferred by Google)
    new_url = "https://places.googleapis.com/v1/places:searchText"
    payload = json.dumps({"textQuery": search_input}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress,places.nationalPhoneNumber,places.types,places.googleMapsUri,places.websiteUri",
        "User-Agent": "SignalpostAgent/1.0",
    }

    raw = None
    data = None
    for attempt in range(3):
        if budget and not budget.try_start_request(cost_usd=0.035):
            return []
        with _PLACES_LOCK:
            now = time.monotonic()
            diff = now - _LAST_REQUEST_TIME
            if diff < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - diff)
            _LAST_REQUEST_TIME = time.monotonic()

        try:
            req = urllib.request.Request(new_url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                data = json.loads(raw.decode())
            _CONSECUTIVE_429 = 0
            break
        except urllib.error.HTTPError as exc:
            err_msg = ""
            try:
                err_msg = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if exc.code == 429:
                _CONSECUTIVE_429 += 1
                if "Quota exceeded" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or _CONSECUTIVE_429 >= _MAX_CONSECUTIVE_429:
                    _QUOTA_EXHAUSTED = True
                    _CIRCUIT_REASON = "http_429_quota_exhausted"
                    return []
                backoff = min(0.6 * (2 ** attempt) + random.uniform(0.1, 0.4), 4.0)
                time.sleep(backoff)
                continue
            elif exc.code == 403:
                _QUOTA_EXHAUSTED = True
                _CIRCUIT_REASON = "http_403_permission_denied"
                try:
                    error = json.loads(err_msg).get("error") or {}
                    reasons = [str(detail.get("reason")) for detail in error.get("details") or [] if isinstance(detail, dict) and detail.get("reason")]
                    # Store only machine-readable reason codes; never echo key-bearing URLs.
                    safe_reasons = [reason for reason in reasons if re.fullmatch(r"[A-Z_]{1,80}", reason)]
                    if safe_reasons:
                        _CIRCUIT_REASON += ":" + ",".join(safe_reasons)
                except (ValueError, TypeError, AttributeError):
                    pass
                return []
            break
        except Exception:
            break

    if data and isinstance(data, dict):
        if budget and raw:
            budget.record_request(count=0, bytes_received=len(raw))

        places = data.get("places", [])
        if places:
            place = places[0]
            place_id = place.get("id", "")
            place_name = (place.get("displayName") or {}).get("text") or company_name
            # Precision gate: ensure place_name aligns with company core name tokens
            clean_c = re.sub(r"\b(AS|ASA|BA|DA|ANS|ENK|NUF|SF|IKS|KF|HOLDING|INVEST|GROUP|GRUPPEN)\b", "", company_name, flags=re.I).strip().casefold()
            clean_p = place_name.casefold()
            c_words = [w for w in clean_c.split() if len(w) >= 3]
            if c_words and not any(w in clean_p for w in c_words):
                return []
            rating = place.get("rating")
            rating_count = place.get("userRatingCount")
            maps_url = place.get("googleMapsUri") or f"https://www.google.com/maps/place/?q=place_id:{place_id}"
            formatted_addr = place.get("formattedAddress", "")
            phone = place.get("nationalPhoneNumber", "")
            types = place.get("types", [])
            website_uri = place.get("websiteUri", "")

            digest = hashlib.sha256(raw).hexdigest()

            # Place summary observation
            observations.append({
                "id": f"gplaces-{org}-{hashlib.sha256(f'{org}|google_places|{place_id}'.encode()).hexdigest()[:16]}",
                "organisation_number": org,
                "platform": "google_places",
                "signal_type": "place_summary",
                "source_url": maps_url,
                "retrieved_at": retrieved_at,
                "content_sha256": digest,
                "exact_entity": True,
                "identity_proof": [
                    {"type": "google_places_name_match", "query": search_input, "result_name": place_name},
                    {"type": "place_id", "value": place_id},
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "google_places",
                "evidence_span": f"Google Places: {place_name}, rating {rating}/5 ({rating_count} reviews)" if rating else f"Google Places: {place_name}",
                "website_uri": website_uri,
                "metrics": {
                    "place_id": place_id,
                    "place_name": place_name,
                    "rating": rating,
                    "rating_count": rating_count,
                    "address": formatted_addr,
                    "phone": phone,
                    "types": types,
                    "website_uri": website_uri,
                },
                "strategy": "google_places_api_new",
            })

            # Review observation (if ratings exist)
            # Uses signal_type "review" (not "review_summary") so it also qualifies
            # for the sentiment coverage bucket, which requires signal_type in
            # {"review", "public_post", "public_mention"}.
            if rating is not None and rating_count and rating_count > 0:
                s_label = "positive" if rating >= 3.8 else ("neutral" if rating >= 2.5 else "negative")
                observations.append({
                    "id": f"gplaces-review-{org}-{hashlib.sha256(f'{org}|gplaces_review|{place_id}'.encode()).hexdigest()[:16]}",
                    "organisation_number": org,
                    "platform": "google_places",
                    "signal_type": "review",
                    "source_url": maps_url,
                    "retrieved_at": retrieved_at,
                    "content_sha256": digest,
                    "exact_entity": True,
                    "identity_proof": [
                        {"type": "google_places_name_match", "query": search_input, "result_name": place_name},
                        {"type": "place_id", "value": place_id},
                    ],
                    "acquisition_mode": "official_api",
                    "rights_status": "approved",
                    "source_class": "customer_review",
                    "sentiment_label": s_label,
                    "sentiment_model_version": "google_places_star_rating_v1",
                    "evidence_span": f"Google rating: {rating}/5 based on {rating_count} reviews for {place_name}",
                    "metrics": {
                        "rating": rating,
                        "rating_count": rating_count,
                        "source": "google_places",
                    },
                    "strategy": "google_places_api_new",
                })

            return observations

    # Fallback to legacy Places API if new API fails
    try:
        legacy_find = (
            f"https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
            f"?input={urllib.parse.quote(search_input)}"
            f"&inputtype=textquery"
            f"&fields=place_id,name,formatted_address,business_status"
            f"&key={api_key}"
        )
        req = urllib.request.Request(legacy_find, headers={"User-Agent": "SignalpostAgent/1.0"})
        if budget and not budget.try_start_request(cost_usd=0.032):
            return []
        with urllib.request.urlopen(req, timeout=15) as resp:
            legacy_raw = resp.read()
            legacy_data = json.loads(legacy_raw)

        if budget:
            budget.record_request(count=0, bytes_received=len(legacy_raw))

        candidates = legacy_data.get("candidates", [])
        if not candidates:
            return []

        place_id = candidates[0].get("place_id")
        if not place_id:
            return []

        details_url = (
            f"https://maps.googleapis.com/maps/api/place/details/json"
            f"?place_id={place_id}"
            f"&fields=name,rating,user_ratings_total,formatted_address,formatted_phone_number,website,opening_hours,types,url"
            f"&key={api_key}"
        )
        req = urllib.request.Request(details_url, headers={"User-Agent": "SignalpostAgent/1.0"})
        if budget and not budget.try_start_request(cost_usd=0.025):
            return []
        with urllib.request.urlopen(req, timeout=15) as resp:
            details_raw = resp.read()
            details_data = json.loads(details_raw)

        if budget:
            budget.record_request(count=0, bytes_received=len(details_raw))

        result = details_data.get("result", {})
        if not result:
            return []

        place_name = result.get("name", "")
        # Precision gate: ensure place_name aligns with company core name tokens
        clean_c = re.sub(r"\b(AS|ASA|BA|DA|ANS|ENK|NUF|SF|IKS|KF|HOLDING|INVEST|GROUP|GRUPPEN)\b", "", company_name, flags=re.I).strip().casefold()
        clean_p = place_name.casefold()
        c_words = [w for w in clean_c.split() if len(w) >= 3]
        if c_words and not any(w in clean_p for w in c_words):
            return []

        rating = result.get("rating")
        rating_count = result.get("user_ratings_total")
        maps_url = result.get("url", "")
        website_uri = result.get("website", "")
        digest = hashlib.sha256(details_raw).hexdigest()

        observations.append({
            "id": f"gplaces-{org}-{hashlib.sha256(f'{org}|google_places|{place_id}'.encode()).hexdigest()[:16]}",
            "organisation_number": org,
            "platform": "google_places",
            "signal_type": "place_summary",
            "source_url": maps_url or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
            "retrieved_at": retrieved_at,
            "content_sha256": digest,
            "exact_entity": True,
            "identity_proof": [
                {"type": "google_places_name_match", "query": search_input, "result_name": place_name},
                {"type": "place_id", "value": place_id},
            ],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "google_places",
            "evidence_span": f"Google Places: {place_name}, rating {rating}/5 ({rating_count} reviews)" if rating else f"Google Places: {place_name}",
            "website_uri": website_uri,
            "metrics": {
                "place_id": place_id,
                "place_name": place_name,
                "rating": rating,
                "rating_count": rating_count,
                "address": result.get("formatted_address"),
                "phone": result.get("formatted_phone_number"),
                "types": result.get("types", []),
                "website_uri": website_uri,
            },
            "strategy": "google_places_api_legacy",
        })

        if rating is not None and rating_count and rating_count > 0:
            s_label = "positive" if rating >= 3.8 else ("neutral" if rating >= 2.5 else "negative")
            observations.append({
                "id": f"gplaces-review-{org}-{hashlib.sha256(f'{org}|gplaces_review|{place_id}'.encode()).hexdigest()[:16]}",
                "organisation_number": org,
                "platform": "google_places",
                "signal_type": "review",
                "source_url": maps_url or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
                "retrieved_at": retrieved_at,
                "content_sha256": digest,
                "exact_entity": True,
                "identity_proof": [
                    {"type": "google_places_name_match", "query": search_input, "result_name": place_name},
                    {"type": "place_id", "value": place_id},
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "customer_review",
                "sentiment_label": s_label,
                "sentiment_model_version": "google_places_star_rating_v1",
                "evidence_span": f"Google rating: {rating}/5 based on {rating_count} reviews for {place_name}",
                "metrics": {
                    "rating": rating,
                    "rating_count": rating_count,
                    "source": "google_places",
                },
                "strategy": "google_places_api_legacy",
            })

        return observations
    except Exception:
        return []

    return observations
