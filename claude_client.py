"""Compatibility shim — all AI calls live in openai_client.

Legacy imports of claude_client continue to work during migration.
Prefer: from openai_client import ...
"""

from openai_client import *  # noqa: F403
from openai_client import DEFAULT_SCREENING_PROMPT, SYSTEM_PROMPT, MAX_TOKENS, MAX_PDFS
