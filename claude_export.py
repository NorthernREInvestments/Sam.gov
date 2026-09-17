"""Compatibility shim — prefer ai_export."""

from ai_export import (  # noqa: F401
    AI_EXPORT_INSTRUCTIONS as CLAUDE_INSTRUCTIONS,
    build_ai_export as build_claude_export,
    export_ai_json_bytes as export_claude_json_bytes,
)
