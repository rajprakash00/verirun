from fastapi.testclient import TestClient


def test_inbox_renders_seeded_messages(maildesk_client: TestClient) -> None:
    response = maildesk_client.get("/")

    assert response.status_code == 200
    assert "Invoice batch for processing" in response.text
    assert "Invoice PP-2026-042 from Paperline Print Shop" in response.text
    assert "New supplier setup — Cascade Fabrication LLC" in response.text


def test_message_view_shows_the_body_and_attachments(maildesk_client: TestClient) -> None:
    response = maildesk_client.get("/messages/MSG-7008")

    assert response.status_code == 200
    assert "PP-2026-042" in response.text
    assert "scan of the paper original" in response.text
    assert "PP-2026-042.pdf" in response.text


def test_attachment_downloads_with_its_original_filename(maildesk_client: TestClient) -> None:
    response = maildesk_client.get("/attachments/ATT-8014/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "PP-2026-042.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


def test_unknown_message_and_attachment_are_404(maildesk_client: TestClient) -> None:
    assert maildesk_client.get("/messages/MSG-9999").status_code == 404
    assert maildesk_client.get("/attachments/ATT-9999/download").status_code == 404


def test_api_lists_messages_and_exposes_attachments(maildesk_client: TestClient) -> None:
    response = maildesk_client.get("/api/messages")

    assert response.status_code == 200
    messages = {message["id"]: message for message in response.json()}
    assert len(messages) == 9
    assert messages["MSG-7001"]["scenario"] == "batch"

    detail = maildesk_client.get("/api/messages/MSG-7001").json()
    assert len(detail["attachments"]) == 7
    assert detail["attachments"][0]["filename"].endswith(".pdf")

    filtered = maildesk_client.get("/api/messages", params={"scenario": "scanned"}).json()
    assert [message["id"] for message in filtered] == ["MSG-7008"]
