"""Competitive channel filter + MSRP-first quote priority."""

from channel_fit.engine import (
    BUILD,
    score_opportunity_channel_fit,
    score_money_sprint_rows,
)
from channel_fit.pre_quote import PRE_QUOTE_DECISIONS

__all__ = [
    "BUILD",
    "score_opportunity_channel_fit",
    "score_money_sprint_rows",
    "PRE_QUOTE_DECISIONS",
]
