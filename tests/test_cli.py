from pathlib import Path

import pytest

from company_operator.cli import main
from company_operator.config import Settings


def test_help_lists_all_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    for command in ("run", "serve", "report"):
        assert command in output


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
    settings = Settings(_env_file=None, run_db=tmp_path / "runs" / "operator.db")

    code = main(["serve", "--host", "0.0.0.0", "--port", "8123"], settings=settings)

    assert code == 0
    assert calls["kwargs"] == {"host": "0.0.0.0", "port": 8123, "log_level": "info"}
    assert calls["app"].title == "Operator"
