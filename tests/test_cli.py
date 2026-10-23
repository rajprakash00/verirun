from pathlib import Path

import pytest

from verirun.cli import main
from verirun.config import Settings


def test_help_lists_all_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    for command in ("run", "serve", "report", "eval", "demo"):
        assert command in output


def test_demo_command_seeds_and_runs_the_demo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "demo",
            "--state-dir",
            str(tmp_path / "state"),
            "--shared",
            str(tmp_path / "shared"),
            "--runs",
            str(tmp_path / "runs"),
            "--no-browser",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Demo complete" in output
    assert (tmp_path / "state" / "ledgerlite.db").is_file()
    assert (tmp_path / "state" / "maildesk.db").is_file()


def test_run_requires_a_request(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["run"])

    assert excinfo.value.code == 2


def test_serve_starts_the_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_run)
    settings = Settings(_env_file=None, run_db=tmp_path / "runs" / "verirun.db")

    code = main(["serve", "--host", "0.0.0.0", "--port", "8123"], settings=settings)

    assert code == 0
    assert calls["kwargs"] == {"host": "0.0.0.0", "port": 8123, "log_level": "info"}
    assert calls["app"].title == "Verirun"
