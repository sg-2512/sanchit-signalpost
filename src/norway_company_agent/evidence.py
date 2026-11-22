from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

EvidenceStatus = Literal[
    "available",
    "not_found",
    "not_applicable",
    "not_fetched",
    "source_error",
    "blocked",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Evidence:
    field: str
    status: EvidenceStatus
    source_type: str
    source_class: str
    source_url: str
    retrieved_at: str
    value: Any = None
    as_of: str | None = None
    note: str | None = None
    content_sha256: str | None = None
    source_row_key: str | None = None
    effective_at: str | None = None
    http_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


import urllib.parse

SECRET_QUERY_PARAMS = {"key", "api_key", "apikey", "secret", "token", "access_token", "auth"}


def sanitize_url(url: Any) -> str:
    """Sanitize secrets, API keys, and auth tokens from URLs recorded in evidence/audit logs."""
    if not url:
        return ""
    url_str = str(url).strip()
    try:
        parsed = urllib.parse.urlparse(url_str)
        if not parsed.query:
            return url_str
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        changed = False
        for param in list(q.keys()):
            if param.lower() in SECRET_QUERY_PARAMS:
                q[param] = ["REDACTED"]
                changed = True
        if not changed:
            return url_str
        clean_query = urllib.parse.urlencode(q, doseq=True)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, parsed.fragment))
    except Exception:
        return url_str


def capture_source_fields(body: Any, pointers: tuple[str, ...]) -> list[dict[str, Any]]:
    """Record JSON pointers and the values actually present in a fetched response."""
    fields = []
    for pointer in pointers:
        value = body
        try:
            for part in pointer.lstrip("/").split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if value is not None:
            fields.append({"source_pointer": pointer, "source_value": value})
    return fields


def observation_provenance(observation: dict[str, Any]) -> dict[str, Any]:
    """Expose excerpts, structured API fields and generated descriptions distinctly."""
    fields = observation.get("source_fields")
    excerpt = observation.get("literal_excerpt")
    return {
        "evidence_kind": "literal_excerpt" if excerpt else ("structured_pointer" if fields else "source_record"),
        "evidence_span": excerpt,
        "source_fields": fields,
        "evidence_description": observation.get("evidence_span") if not excerpt else None,
    }


def evidence(
    field: str,
    status: EvidenceStatus,
    source_type: str,
    source_url: str,
    *,
    value: Any = None,
    as_of: str | None = None,
    note: str | None = None,
    retrieved_at: str | None = None,
    content_sha256: str | None = None,
    source_row_key: str | None = None,
    effective_at: str | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    return Evidence(
        field=field,
        status=status,
        source_type=source_type,
        source_class=source_type,
        source_url=sanitize_url(source_url),
        retrieved_at=retrieved_at or utc_now(),
        value=value,
        as_of=as_of,
        note=note,
        content_sha256=content_sha256,
        source_row_key=source_row_key,
        effective_at=effective_at,
        http_status=http_status,
    ).to_dict()
