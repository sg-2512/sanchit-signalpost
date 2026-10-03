"""SignalPost Strategy Experimentation Engine & Learning Harness."""

# Lazy export to avoid RuntimeWarning when executing submodules via `python -m strategies.<submodule>`

from .base import (
    AttemptStatus,
    ExecutionContext,
    RouteCategory,
    StrategyAttemptResult,
    StrategyRoute,
)
from .registry import (
    DEFAULT_REGISTRY,
    StrategyRegistry,
    build_default_registry,
)
from .attempts import (
    AcceptedClaim,
    AttemptStorage,
    ExactIdentityEvidence,
    RejectedClaim,
    StrategyAttemptRecord,
    get_default_attempt_storage,
    validate_attempt_record,
)


def __getattr__(name: str):
    if name in {"FreezeManifest", "VerificationResult", "freeze_strategies", "generate_version_table", "verify_strategies"}:
        from . import freeze
        return getattr(freeze, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AttemptStatus",
    "RouteCategory",
    "ExecutionContext",
    "StrategyAttemptResult",
    "StrategyRoute",
    "DEFAULT_REGISTRY",
    "StrategyRegistry",
    "build_default_registry",
    "AcceptedClaim",
    "AttemptStorage",
    "ExactIdentityEvidence",
    "RejectedClaim",
    "StrategyAttemptRecord",
    "get_default_attempt_storage",
    "validate_attempt_record",
    "FreezeManifest",
    "VerificationResult",
    "freeze_strategies",
    "generate_version_table",
    "verify_strategies",
]
