"""SignalPost Evaluation and Promotion Engine Package."""

from .promotion_gate import (
    GateResult,
    GateStatus,
    GateVerdict,
    PromotionVerdictCode,
    evaluate_promotion,
)


def __getattr__(name: str):
    if name == "evaluate_benchmark":
        from .run import evaluate_benchmark
        return evaluate_benchmark
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GateResult",
    "GateStatus",
    "GateVerdict",
    "PromotionVerdictCode",
    "evaluate_promotion",
    "evaluate_benchmark",
]
