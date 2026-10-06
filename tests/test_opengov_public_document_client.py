"""Unit tests for OpenGov public document client (no network)."""
from opengov_discovery.public_document_client import (
    DOCUMENTS_FOUND_PUBLIC,
    INVALID_PROJECT,
    extract_documents_from_project,
)


def test_extract_documents_from_project_payload():
    payload = {
        "id": 1,
        "title": "Widget Supply ITB",
        "financialId": "ITB-1",
        "attachments": [
            {
                "id": 9,
                "url": "https://government-project.s3.us-west-2.amazonaws.com/1/a.pdf",
                "filename": "ITB-1_Bid_Schedule.pdf",
                "fileExtension": "pdf",
                "type": "other",
                "name": "Bid Schedule",
            }
        ],
        "documentAttachment": {
            "url": "https://government-project.s3.us-west-2.amazonaws.com/1/snap.pdf",
            "filename": "project_document_snapshot.pdf",
            "fileExtension": "pdf",
            "type": "projectDocument",
        },
        "addendums": [],
    }
    docs = extract_documents_from_project(payload)
    assert len(docs) == 2
    assert any(d["document_type"] == "pricing_schedule" for d in docs)
    assert any(d["document_type"] == "solicitation_packet" for d in docs)


def test_invalid_project_id():
    from opengov_discovery.public_document_client import OpenGovPublicDocumentClient

    with OpenGovPublicDocumentClient() as client:
        out = client.fetch_project_documents(project_id="abc")
    assert out["status"] == INVALID_PROJECT
    assert out["document_count"] == 0


def test_status_constants_exported():
    assert DOCUMENTS_FOUND_PUBLIC == "DOCUMENTS_FOUND_PUBLIC"
