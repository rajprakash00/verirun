"""A tiny in-process IMAP4 server for adapter tests.

It speaks the subset ``imaplib`` needs for this adapter: CAPABILITY, LOGIN,
SELECT, UID SEARCH, UID FETCH, UID STORE, and LOGOUT. Messages are raw RFC822
bytes, the protocol is plain TCP so no certificate is needed, and the server
records the credentials it was given.
"""

from __future__ import annotations

import shlex
import socketserver
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self


@dataclass
class ServerMessage:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)


class ImapTestServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        *,
        username: str = "ap@company.example",
        password: str = "app-password",
        uidvalidity: int = 1,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _ImapHandler)
        self.username = username
        self.password = password
        self.uidvalidity = uidvalidity
        self.messages: list[ServerMessage] = []
        self.logins: list[tuple[str, str]] = []
        self.searches: list[str] = []
        self.fetches: list[int] = []
        self.fail_select = False
        self.send_uidvalidity = True
        self.fail_search = False
        self.fail_fetch = False
        self.hide_fetch_uids: set[int] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def add_message(self, raw: bytes | str | Path) -> ServerMessage:
        if isinstance(raw, Path):
            data = raw.read_bytes()
        elif isinstance(raw, str):
            data = raw.encode("utf-8")
        else:
            data = raw
        with self._lock:
            message = ServerMessage(uid=len(self.messages) + 1, raw=data)
            self.messages.append(message)
        return message

    def add_mailbox(self, directory: str | Path) -> list[ServerMessage]:
        """Add every .eml in a fixture mailbox directory, in name order."""
        return [
            self.add_message(path)
            for path in sorted(Path(directory).glob("*.eml"), key=lambda item: item.name)
        ]

    def bump_uidvalidity(self) -> int:
        """Start a new UID epoch, the way a real server does after a rebuild."""
        with self._lock:
            self.uidvalidity += 1
        return self.uidvalidity

    def start(self) -> ImapTestServer:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class _ImapHandler(socketserver.StreamRequestHandler):
    server: ImapTestServer

    def handle(self) -> None:
        self.wfile.write(b"* OK IMAP4rev1 test server ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            decoded = line.decode("utf-8", "replace").rstrip("\r\n")
            if not decoded:
                continue
            parts = decoded.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, command = parts[0], parts[1].upper()
            rest = parts[2] if len(parts) > 2 else ""
            if command == "UID":
                if not self._uid(tag, rest):
                    return
                continue
            if command == "LOGOUT":
                self.wfile.write(b"* BYE logging out\r\n")
                self._ok(tag, "LOGOUT completed")
                return
            if command == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n")
                self._ok(tag, "CAPABILITY completed")
            elif command == "LOGIN":
                if self._login(rest):
                    self._ok(tag, "LOGIN completed")
                else:
                    self.wfile.write(b"* NO [AUTHENTICATIONFAILED] bad credentials\r\n")
                    self.wfile.write(f"{tag} NO LOGIN failed\r\n".encode())
            elif command in ("SELECT", "EXAMINE"):
                self._select(tag, rest)
            elif command == "NOOP":
                self._ok(tag, "NOOP completed")
            elif command in ("CHECK", "CLOSE"):
                self._ok(tag, f"{command} completed")
            else:
                self.wfile.write(f"{tag} BAD unsupported command {command}\r\n".encode())

    def _uid(self, tag: str, rest: str) -> bool:
        parts = shlex.split(rest)
        if not parts:
            return True
        subcommand = parts[0].upper()
        if subcommand == "SEARCH":
            if self.server.fail_search:
                self.wfile.write(f"{tag} NO SEARCH failed\r\n".encode())
                return True
            self._search(parts[1:])
        elif subcommand == "FETCH":
            if self.server.fail_fetch:
                self.wfile.write(f"{tag} NO FETCH failed\r\n".encode())
                return True
            self._fetch(parts[1:])
        elif subcommand == "STORE":
            self._store(parts[1:])
        else:
            self.wfile.write(f"{tag} BAD unsupported UID command {subcommand}\r\n".encode())
            return True
        self._ok(tag, f"UID {subcommand} completed")
        return True

    def _login(self, rest: str) -> bool:
        try:
            values = shlex.split(rest)
        except ValueError:
            return False
        if len(values) < 2:
            return False
        username, password = values[0], values[1]
        self.server.logins.append((username, password))
        return username == self.server.username and password == self.server.password

    def _select(self, tag: str, rest: str) -> None:
        try:
            values = shlex.split(rest)
        except ValueError:
            values = []
        mailbox = values[0] if values else ""
        if self.server.fail_select:
            self.wfile.write(f"{tag} NO SELECT failed\r\n".encode())
            return
        if mailbox.upper() != "INBOX":
            self.wfile.write(f"{tag} NO [NONEXISTENT] no such mailbox\r\n".encode())
            return
        self.wfile.write(f"* {len(self.server.messages)} EXISTS\r\n".encode())
        self.wfile.write(b"* FLAGS (\\Seen)\r\n")
        if self.server.send_uidvalidity:
            self.wfile.write(
                f"* OK [UIDVALIDITY {self.server.uidvalidity}] UIDs valid\r\n".encode()
            )
        uidnext = len(self.server.messages) + 1
        self.wfile.write(f"* OK [UIDNEXT {uidnext}] Predicted next UID\r\n".encode())
        self._ok(tag, "SELECT completed", code="READ-WRITE")

    def _search(self, parts: list[str]) -> None:
        spec = parts[-1] if parts else "1:*"
        self.server.searches.append(spec)
        wanted = self._uids(spec)
        numbers = " ".join(str(uid) for uid in sorted(wanted))
        self.wfile.write(f"* SEARCH {numbers}\r\n".encode())

    def _fetch(self, parts: list[str]) -> None:
        spec = parts[0] if parts else "1:*"
        for index, message in enumerate(self.server.messages, start=1):
            if message.uid not in self._uids(spec):
                continue
            if message.uid in self.server.hide_fetch_uids:
                continue
            self.server.fetches.append(message.uid)
            prefix = (
                f"* {index} FETCH (UID {message.uid} "
                f'INTERNALDATE "05-Sep-2026 09:00:00 +0000" '
                f"BODY[] {{{len(message.raw)}}}\r\n"
            )
            self.wfile.write(prefix.encode())
            self.wfile.write(message.raw)
            self.wfile.write(b")\r\n")

    def _store(self, parts: list[str]) -> None:
        if not parts:
            return
        uid = int(parts[0])
        for index, message in enumerate(self.server.messages, start=1):
            if message.uid != uid:
                continue
            message.flags.add("\\Seen")
            self.wfile.write(
                f"* {index} FETCH (UID {uid} FLAGS (\\Seen))\r\n".encode()
            )

    def _uids(self, spec: str) -> set[int]:
        highest = len(self.server.messages)
        selected: set[int] = set()
        for part in spec.split(","):
            if ":" in part:
                start_text, end_text = part.split(":", 1)
                start = highest if start_text == "*" else int(start_text)
                end = highest if end_text == "*" else int(end_text)
                selected.update(range(min(start, end), max(start, end) + 1))
            elif part != "*":
                selected.add(int(part))
            else:
                selected.add(highest)
        return {uid for uid in selected if 1 <= uid <= highest}

    def _ok(self, tag: str, text: str, *, code: str | None = None) -> None:
        prefix = f"[{code}] " if code else ""
        self.wfile.write(f"{tag} OK {prefix}{text}\r\n".encode())
