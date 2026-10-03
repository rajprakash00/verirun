import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from mocks.seed import scenarios
from mocks.seed.maildesk import reset_and_seed


def fetch_all(db_path: Path, table: str) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def test_seed_creates_the_shared_file_tree(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"

    reset_and_seed(tmp_path / "maildesk.db", shared_root)

    for name in ("documents", "archive", "processed"):
        assert (shared_root / name).is_dir(), name


def test_seed_creates_one_message_per_scenario_plus_requests(tmp_path: Path) -> None:
    reset_and_seed(tmp_path / "maildesk.db", tmp_path / "shared")

    messages = fetch_all(tmp_path / "maildesk.db", "messages")

    assert len(messages) == 11
    assert {message["scenario"] for message in messages} == {
        *[scenario.key for scenario in scenarios.SCENARIOS if scenario.key != scenarios.HAPPY.key],
        "batch",
        *[vendor.key for vendor in scenarios.VENDOR_SCENARIOS],
    }


def test_failure_scenario_messages_carry_their_scenario_attachment(tmp_path: Path) -> None:
    reset_and_seed(tmp_path / "maildesk.db", tmp_path / "shared")

    attachments = fetch_all(tmp_path / "maildesk.db", "attachments")
    by_message: dict[str, list[str]] = {}
    for attachment in attachments:
        by_message.setdefault(attachment["message_id"], []).append(attachment["filename"])

    messages = fetch_all(tmp_path / "maildesk.db", "messages")
    for message in messages:
        if message["scenario"] == "batch":
            continue
        if message["scenario"] in scenarios.VENDOR_SCENARIOS_BY_KEY:
            vendor = scenarios.VENDOR_SCENARIOS_BY_KEY[message["scenario"]]
            expected = [vendor.tax_form_filename] if vendor.tax_form_filename else []
        else:
            invoice = scenarios.SCENARIOS_BY_KEY[message["scenario"]]
            expected = [f"{invoice.invoice_number}.pdf"]
        assert by_message.get(message["id"], []) == expected


def test_batch_message_attaches_the_text_layer_invoices(tmp_path: Path) -> None:
    reset_and_seed(tmp_path / "maildesk.db", tmp_path / "shared")

    messages = fetch_all(tmp_path / "maildesk.db", "messages")
    batch = next(message for message in messages if message["scenario"] == "batch")
    attachments = [
        attachment
        for attachment in fetch_all(tmp_path / "maildesk.db", "attachments")
        if attachment["message_id"] == batch["id"]
    ]

    filenames = {attachment["filename"] for attachment in attachments}
    assert len(attachments) == 7
    assert f"{scenarios.SCANNED.invoice_number}.pdf" not in filenames
    assert f"{scenarios.HAPPY.invoice_number}.pdf" in filenames


def test_reseeding_resets_the_db_and_tree_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "maildesk.db"
    shared_root = tmp_path / "shared"
    reset_and_seed(db_path, shared_root)
    (shared_root / "archive" / "leftover.txt").write_text("leftover")

    reset_and_seed(db_path, shared_root)
    first_messages = [tuple(row) for row in fetch_all(db_path, "messages")]
    first_files = file_snapshot(shared_root)

    reset_and_seed(db_path, shared_root)

    assert not (shared_root / "archive" / "leftover.txt").exists()
    assert [tuple(row) for row in fetch_all(db_path, "messages")] == first_messages
    assert file_snapshot(shared_root) == first_files


def file_snapshot(shared_root: Path) -> list[tuple[str, str]]:
    return sorted(
        (path.relative_to(shared_root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in shared_root.rglob("*")
        if path.is_file()
    )


def test_every_attachment_points_at_a_shared_tree_file(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"
    reset_and_seed(tmp_path / "maildesk.db", shared_root)

    attachments = fetch_all(tmp_path / "maildesk.db", "attachments")
    assert len(attachments) == 16
    for attachment in attachments:
        path = shared_root / attachment["path"]
        assert path.is_file(), attachment["path"]
        assert path.name == attachment["filename"]
        assert path.stat().st_size == attachment["size_bytes"]
