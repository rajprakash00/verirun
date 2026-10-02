from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from mocks.maildesk import db


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _select(
    conn: sqlite3.Connection,
    table: str,
    filters: dict[str, str | None],
) -> list[dict]:
    clauses = []
    params: list[str] = []
    for column, value in filters.items():
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = f"SELECT * FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    return [dict(row) for row in conn.execute(sql, params)]


def _get_one(conn: sqlite3.Connection, table: str, row_id: str) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{table[:-1]} {row_id} not found")
    return dict(row)


def _inbox_messages(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT messages.*, COUNT(attachments.id) AS attachment_count
            FROM messages
            LEFT JOIN attachments ON attachments.message_id = messages.id
            WHERE messages.folder = 'inbox'
            GROUP BY messages.id
            ORDER BY messages.received_at DESC, messages.id DESC
            """
        )
    ]


def create_app(db_path: Path, shared_root: Path) -> FastAPI:
    app = FastAPI(title="MailDesk", version="0.1.0")
    app.state.db_path = Path(db_path)
    app.state.shared_root = Path(shared_root)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/messages")
    def list_messages(
        conn: Conn,
        folder: str | None = None,
        scenario: str | None = None,
    ) -> list[dict]:
        return _select(conn, "messages", {"folder": folder, "scenario": scenario})

    @app.get("/api/messages/{message_id}")
    def get_message(message_id: str, conn: Conn) -> dict:
        message = _get_one(conn, "messages", message_id)
        message["attachments"] = _select(conn, "attachments", {"message_id": message_id})
        return message

    @app.get("/api/attachments/{attachment_id}")
    def get_attachment(attachment_id: str, conn: Conn) -> dict:
        return _get_one(conn, "attachments", attachment_id)

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    def render(request: Request, name: str, context: dict | None = None) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context or {})

    @app.get("/", response_class=HTMLResponse)
    def inbox_page(request: Request, conn: Conn) -> HTMLResponse:
        return render(request, "inbox.html", {"messages": _inbox_messages(conn)})

    @app.get("/messages/{message_id}", response_class=HTMLResponse)
    def message_detail_page(request: Request, message_id: str, conn: Conn) -> HTMLResponse:
        message = _get_one(conn, "messages", message_id)
        attachments = _select(conn, "attachments", {"message_id": message_id})
        return render(
            request,
            "message.html",
            {"message": message, "attachments": attachments},
        )

    @app.get("/attachments/{attachment_id}/download")
    def download_attachment(attachment_id: str, conn: Conn) -> FileResponse:
        attachment = _get_one(conn, "attachments", attachment_id)
        path = Path(app.state.shared_root) / attachment["path"]
        if not path.is_file():
            raise HTTPException(
                status_code=404, detail=f"attachment file {attachment['path']} not found"
            )
        return FileResponse(
            path,
            media_type=attachment["content_type"],
            filename=attachment["filename"],
        )

    return app


DEFAULT_DB = Path("mocks/state/maildesk.db")
DEFAULT_SHARED_ROOT = Path("shared")

app = create_app(DEFAULT_DB, DEFAULT_SHARED_ROOT)
