import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_recovery import cli
from telegram_recovery.database import Database

CHANNEL = "-1001234567890"


def test_help_and_reserved_command_parsing(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    for command in ("auth", "inventory", "download", "verify", "report", "status"):
        assert command in help_text
    # Parse only here; authentication itself is covered by offline mocks in test_auth.py.
    assert cli.parser().parse_args(["auth"]).command == "auth"
    assert cli.parser().parse_args(["download", "--channel", CHANNEL]).concurrency == 1


@pytest.mark.parametrize(
    "options",
    [
        ["--limit", "0"],
        ["--page-size", "101"],
        ["--channel", "123"],
    ],
)
def test_invalid_options_never_open_session(monkeypatch, options):
    session = Mock(side_effect=AssertionError("Should not connect"))
    monkeypatch.setattr(cli, "existing_user_session", session)
    with pytest.raises(SystemExit) as exc:
        cli.main(["inventory", "--channel", CHANNEL, *options])
    assert exc.value.code == 2
    session.assert_not_called()


def test_invalid_tag_never_opens_session(monkeypatch, capsys):
    session = Mock(side_effect=AssertionError("Should not connect"))
    monkeypatch.setattr(cli, "existing_user_session", session)
    assert cli.main(["inventory", "--channel", CHANNEL, "--tag", "invalid"]) == 2
    session.assert_not_called()
    assert "inválido" in capsys.readouterr().err


def test_missing_session_local_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("No prompt allowed")))
    assert cli.main(["inventory", "--root", str(tmp_path), "--channel", CHANNEL]) == 1
    assert "Sessão local ausente" in capsys.readouterr().err
    assert not (tmp_path / "data" / "manifest.sqlite3").exists()


def test_inventory_status_report_end_to_end_offline(
    tmp_path, monkeypatch, capsys, message_factory, client_factory
):
    fake = client_factory([message_factory(1), message_factory(2, "Sem tag")])

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield fake

    monkeypatch.setattr(cli, "existing_user_session", session)
    monkeypatch.setattr(cli, "resolve_channel", AsyncMock(return_value="entity"))
    args = ["inventory", "--root", str(tmp_path), "--channel", CHANNEL, "--limit", "2"]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert "novos=2" in captured.out and "sem_tag=1" in captured.out
    assert "Lidas=2" in captured.err
    assert not (tmp_path / "downloads").exists()
    monkeypatch.setattr(
        cli, "existing_user_session", Mock(side_effect=AssertionError("No network"))
    )
    assert cli.main(["status", "--root", str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["videos"] == 2 and summary["without_tags"] == 1
    assert summary["statuses"] == {"inventoried": 2}
    assert cli.main(["report", "--root", str(tmp_path), "--channel", CHANNEL]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["tags"] == ["F2072"]
    assert records[1]["tags"] == []


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_raw_exception_is_never_printed(tmp_path, monkeypatch, capsys, error_type):
    @asynccontextmanager
    async def failing(*args, **kwargs):
        raise error_type("api_hash=secret phone=private code=123456")
        yield

    monkeypatch.setattr(cli, "existing_user_session", failing)
    assert cli.main(["inventory", "--root", str(tmp_path), "--channel", CHANNEL]) == 1
    captured = capsys.readouterr()
    assert "secret" not in captured.err
    assert "private" not in captured.err
    assert "123456" not in captured.err
    assert "Traceback" not in captured.err


def test_empty_status_and_future_schema(tmp_path, capsys):
    assert cli.main(["status", "--root", str(tmp_path)]) == 0
    assert "ainda não criado" in capsys.readouterr().out
    assert not (tmp_path / "data").exists()
    path = tmp_path / "data" / "manifest.sqlite3"
    with Database(path) as db:
        db.connection.execute("PRAGMA user_version=99")
    assert cli.main(["status", "--root", str(tmp_path)]) == 1
    assert "Versão do manifesto" in capsys.readouterr().err
