"""Central OpenAI model router — callers request a tier/stage, not hard-coded model names."""

from __future__ import annotations

import os
from enum import Enum

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

# Back-compat single override (legacy OPENAI_MODEL). Prefer tier env vars.
DEFAULT_CHEAP = "gpt-5.6-luna"
DEFAULT_STANDARD = "gpt-5.6-terra"
DEFAULT_PREMIUM = "gpt-5.6-sol"


class ModelTier(str, Enum):
    CHEAP = "cheap"
    STANDARD = "standard"
    PREMIUM = "premium"


class FunnelStage(int, Enum):
    """Opportunity funnel stages. Stage 0 is free (no OpenAI)."""

    STAGE_0 = 0
    STAGE_1 = 1
    STAGE_2 = 2
    STAGE_3 = 3
    STAGE_4 = 4
    STAGE_5 = 5


# Default tier per funnel stage (Stage 5 is never auto-selected).
STAGE_DEFAULT_TIER: dict[FunnelStage, ModelTier] = {
    FunnelStage.STAGE_1: ModelTier.CHEAP,
    FunnelStage.STAGE_2: ModelTier.CHEAP,
    FunnelStage.STAGE_3: ModelTier.CHEAP,
    FunnelStage.STAGE_4: ModelTier.STANDARD,
    FunnelStage.STAGE_5: ModelTier.PREMIUM,
}


def model_for_tier(tier: ModelTier | str) -> str:
    t = ModelTier(tier) if not isinstance(tier, ModelTier) else tier
    if t is ModelTier.CHEAP:
        return (os.getenv("OPENAI_MODEL_CHEAP") or DEFAULT_CHEAP).strip() or DEFAULT_CHEAP
    if t is ModelTier.STANDARD:
        return (os.getenv("OPENAI_MODEL_STANDARD") or DEFAULT_STANDARD).strip() or DEFAULT_STANDARD
    if t is ModelTier.PREMIUM:
        return (os.getenv("OPENAI_MODEL_PREMIUM") or DEFAULT_PREMIUM).strip() or DEFAULT_PREMIUM
    raise ValueError(f"Unknown model tier: {tier}")


def tier_for_stage(stage: FunnelStage | int) -> ModelTier:
    s = FunnelStage(int(stage))
    if s is FunnelStage.STAGE_0:
        raise ValueError("Stage 0 must not route to any OpenAI model")
    return STAGE_DEFAULT_TIER[s]


def resolve_model(
    *,
    stage: FunnelStage | int | None = None,
    tier: ModelTier | str | None = None,
    model: str | None = None,
) -> str:
    """
    Single central router.
    Priority: explicit model override → tier → stage default → legacy OPENAI_MODEL → cheap.
    """
    if model and str(model).strip():
        return str(model).strip()
    if tier is not None:
        return model_for_tier(tier)
    if stage is not None:
        return model_for_tier(tier_for_stage(stage))
    legacy = (os.getenv("OPENAI_MODEL") or "").strip()
    if legacy:
        return legacy
    return model_for_tier(ModelTier.CHEAP)


def web_search_allowed_for_stage(stage: FunnelStage | int | None) -> bool:
    """Web search only Stage 3+."""
    if stage is None:
        return False
    return int(stage) >= FunnelStage.STAGE_3


def stage_allows_automatic(stage: FunnelStage | int | None) -> bool:
    """Stage 5 requires explicit user action — never automatic."""
    if stage is None:
        return True
    return int(stage) != FunnelStage.STAGE_5


# Compact output caps by stage (tokens). Stage 1 must stay tiny.
STAGE_MAX_OUTPUT_TOKENS: dict[int, int] = {
    1: int(os.getenv("AI_STAGE1_MAX_OUTPUT_TOKENS", "400")),
    2: int(os.getenv("AI_STAGE2_MAX_OUTPUT_TOKENS", "4400")),
    3: 2000,
    4: 4096,
    5: 8192,
}

# Approximate max input characters by stage (pre-call gate).
STAGE_MAX_INPUT_CHARS: dict[int, int] = {
    1: int(os.getenv("AI_STAGE1_MAX_INPUT_CHARS", "12000")),
    2: int(os.getenv("AI_STAGE2_MAX_INPUT_CHARS", "80000")),
    3: int(os.getenv("AI_STAGE3_MAX_INPUT_CHARS", "60000")),
    4: int(os.getenv("AI_STAGE4_MAX_INPUT_CHARS", "120000")),
    5: int(os.getenv("AI_STAGE5_MAX_INPUT_CHARS", "200000")),
}


def clamp_max_output_tokens(stage: FunnelStage | int | None, requested: int) -> int:
    if stage is None:
        return max(1, requested)
    cap = STAGE_MAX_OUTPUT_TOKENS.get(int(stage))
    if cap is None:
        return max(1, requested)
    return max(1, min(int(requested), cap))


def max_input_chars_for_stage(stage: FunnelStage | int | None) -> int | None:
    if stage is None:
        return None
    return STAGE_MAX_INPUT_CHARS.get(int(stage))
