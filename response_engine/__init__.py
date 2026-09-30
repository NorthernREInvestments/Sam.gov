"""M3 Response Engine — R1 Solicitation Response Compiler foundation."""

from response_engine.constants import BUILD
from response_engine.service import (
    apply_amendment_quantity_change,
    bid_prep_card_for_opportunity,
    compile_project,
    create_or_get_project_from_opportunity,
    find_by_opportunity,
    get_project_view,
    ingest_document,
    ingest_document_set,
    load_project,
    save_project,
)

__all__ = [
    "BUILD",
    "create_or_get_project_from_opportunity",
    "compile_project",
    "apply_amendment_quantity_change",
    "get_project_view",
    "bid_prep_card_for_opportunity",
    "ingest_document",
    "ingest_document_set",
    "load_project",
    "save_project",
    "find_by_opportunity",
]
