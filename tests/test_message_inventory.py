import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_recovery import cli
from telegram_recovery.database import Database
from telegram_recovery.discovery import Discovery
from telegram_recovery.inventory import InventoryFilter, extract_video, inventory_message
from telegram_recovery.security import RecoveryError
from telegram_recovery.telegram_client import RetryPolicy

CHANNEL = -1001234567890
CAPTION = "#F2072 aula\n\n116 - Redes\n=001 - Protocolos\n==005 - Aula privada"


def single(client, db, **kwargs):
    return asyncio.run(inventory_message(client, "entity", CHANNEL, db, 2074, **kwargs))


def test_single_message_updates_metadata_without_touching_any_checkpoint(tmp_path, message_factory):
    message = message_factory(2074, CAPTION)
    message.chat_id = CHANNEL
    client = Mock(get_messages=AsyncMock(return_value=message))
    with Database(tmp_path / "manifest.sqlite3") as db:
        scope = InventoryFilter().scope
        db.set_scan(CHANNEL, scope, 100, "limited")
        db.set_scan(CHANNEL, InventoryFilter(tags=("F2072",)).scope, 50, "idle")
        checkpoints = db.summary()["scans"]
        first = single(client, db)
        assert (first.outcome, first.inserted, first.updated) == ("video", 1, 0)
        record = list(db.videos())[0]
        assert record["title"] == "Aula privada"
        assert "116_Redes__001__005_Aula_privada" in record["suggested_filename"]
        assert record["caption"] == CAPTION and record["original_filename"] == "aula.mp4"
        second = single(client, db)
        assert (second.inserted, second.updated) == (0, 1)
        assert list(db.videos())[0]["first_seen_at"] == record["first_seen_at"]
        assert db.summary()["scans"] == checkpoints
        assert db.cursor(CHANNEL, scope) == 100
    for call in client.get_messages.call_args_list:
        assert call.args == ("entity",) and call.kwargs == {"ids": 2074}


@pytest.mark.parametrize("outcome", ["missing", "non_video"])
def test_unavailable_message_preserves_existing_record_and_no_scan(
    tmp_path, message_factory, outcome
):
    message = message_factory(2074, document=False)
    message.chat_id = CHANNEL
    client = Mock(get_messages=AsyncMock(return_value=None if outcome == "missing" else message))
    with Database(tmp_path / "manifest.sqlite3") as db:
        db.save_records([extract_video(message_factory(2074), CHANNEL)])
        before = list(db.videos())
        result = single(client, db)
        assert result.outcome == outcome
        assert list(db.videos()) == before
        assert db.summary()["scans"] == []


@pytest.mark.parametrize("wrong_field,value", [("id", 1), ("chat_id", CHANNEL - 1)])
def test_mismatched_message_never_written(tmp_path, message_factory, wrong_field, value):
    message = message_factory(2074)
    message.chat_id = CHANNEL
    setattr(message, wrong_field, value)
    with Database(tmp_path / "manifest.sqlite3") as db:
        client = Mock(get_messages=AsyncMock(return_value=message))
        with pytest.raises(RecoveryError) as error:
            single(client, db)
        assert error.value.code == "message_mismatch"
        assert list(db.videos()) == [] and db.summary()["scans"] == []


def test_individual_timeout_preserves_manifest(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        db.save_records([extract_video(message_factory(2074), CHANNEL)])
        before = list(db.videos())
        with pytest.raises(RecoveryError):
            single(
                Mock(get_messages=AsyncMock(side_effect=TimeoutError("PRIVATE"))),
                db,
                retry_policy=RetryPolicy(attempts=1),
            )
        assert list(db.videos()) == before


def test_single_cli_log_is_private_and_distinct_from_history(
    tmp_path, monkeypatch, message_factory, capsys
):
    message = message_factory(2074, CAPTION)
    message.chat_id = CHANNEL
    fake = Mock(get_messages=AsyncMock(return_value=message))

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield fake

    monkeypatch.setattr(cli, "existing_user_session", session)
    monkeypatch.setattr(cli, "resolve_channel", AsyncMock(return_value="entity"))
    assert (
        cli.main(
            [
                "inventory",
                "--root",
                str(tmp_path),
                "--channel",
                str(CHANNEL),
                "--message-id",
                "2074",
                "--verbose",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "resultado=video" in captured.out
    (log,) = (tmp_path / "logs").glob("*.jsonl")
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0]["message_id"] == 2074
    assert "Aula privada" not in log.read_text() + captured.out + captured.err
    assert "message_inventory_finished" in {row["event"] for row in rows}
    assert "inventory_finished" not in {row["event"] for row in rows}
    assert rows[-1]["last_committed_cursor"] is None
    assert rows[-1]["status"] == "success"
    assert not (tmp_path / "downloads").exists()


@pytest.mark.parametrize(
    "options",
    [
        ["--limit", "1"],
        ["--rescan"],
        ["--tag", "F2072"],
        ["--from-tag", "F2072"],
        ["--to-tag", "F2073"],
        ["--page-size", "1"],
    ],
)
def test_single_conflicting_options_fail_before_connecting(tmp_path, monkeypatch, options):
    session = Mock(side_effect=AssertionError("Must not connect"))
    monkeypatch.setattr(cli, "existing_user_session", session)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "inventory",
                "--root",
                str(tmp_path),
                "--channel",
                str(CHANNEL),
                "--message-id",
                "2074",
                *options,
            ]
        )
    assert error.value.code == 2
    session.assert_not_called()
    assert not (tmp_path / "logs").exists()


def test_discovery_counts_structured_captions_without_storing_titles(message_factory):
    stats = Discovery()
    record = extract_video(message_factory(2074, CAPTION), CHANNEL)
    stats.add([record, replace(record, caption="#F2073 Outra aula")], 2, 2)
    assert stats.snapshot()["counts"]["structured_captions"] == 1
    assert "Aula privada" not in json.dumps(stats.snapshot())
