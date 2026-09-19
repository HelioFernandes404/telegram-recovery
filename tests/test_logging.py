import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors

from telegram_recovery import cli, telegram_client
from telegram_recovery.database import Database
from telegram_recovery.discovery import Discovery
from telegram_recovery.inventory import InventoryFilter, extract_video, inventory_channel
from telegram_recovery.run_logging import ACTIVE_RUN, RunLog, emit, error_details
from telegram_recovery.security import RecoveryError
from telegram_recovery.telegram_client import read_with_retry, resolve_channel

CHANNEL = -1001234567890


def log_rows(root):
    (path,) = (root / "logs").glob("*.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


def event(rows, name):
    return next(row for row in rows if row["event"] == name)


def test_private_logs_are_unique_and_flush_immediately(tmp_path):
    for _ in range(2):
        with RunLog(tmp_path) as run:
            emit("inventory_started", cursor=4, rescan=False)
            # Read while the writer is open, as tail -f would.
            row = json.loads(run.path.read_text())
            assert row["cursor"] == 4
            assert run.path.stat().st_mode & 0o777 == 0o600
        assert ACTIVE_RUN.get() is None
    assert len(list((tmp_path / "logs").glob("*.jsonl"))) == 2
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700


def test_field_allowlist_and_verbose_do_not_serialize_secrets(tmp_path, capsys):
    class PrivateObject:
        def __repr__(self):
            pytest.fail("A private object was formatted")

    with RunLog(tmp_path, verbose=True):
        emit(
            "page_committed",
            cursor=7,
            caption="SECRET_CAPTION",
            api_hash="SECRET_HASH",
            phone="SECRET_PHONE",
            session=PrivateObject(),
            document=PrivateObject(),
            counts={"api_hash": "SECRET_HASH"},
            mime_counts={"video/SECRET_HASH": 1},
        )
    text = "\n".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    captured = capsys.readouterr()
    assert "SECRET" not in text + captured.out + captured.err
    page = event(log_rows(tmp_path), "page_committed")
    assert "caption" not in page and "counts" not in page and "mime_counts" not in page
    assert page["cursor"] == 7


def test_missing_session_has_actionable_log_without_network(tmp_path, monkeypatch, capsys):
    factory = Mock(side_effect=AssertionError("No Telegram client may be constructed"))
    monkeypatch.setattr(telegram_client, "TelegramClient", factory)
    assert (
        cli.main(
            [
                "inventory",
                "--root",
                str(tmp_path),
                "--channel",
                str(CHANNEL),
                "--limit",
                "100",
            ]
        )
        == 1
    )
    factory.assert_not_called()
    rows = log_rows(tmp_path)
    assert rows[0]["event"] == "run_started"
    assert rows[0]["limit"] == 100
    assert rows[0]["python_version"]
    assert isinstance(rows[0]["ffprobe_available"], bool)
    assert event(rows, "preflight")["session_exists"] is False
    assert rows[-1]["error_code"] == "session_missing"
    assert rows[-1]["read_calls"] == 0
    assert rows[-1]["last_committed_cursor"] is None
    assert rows[-1]["exit_code"] == 1
    assert "session_missing" in capsys.readouterr().err


def test_successful_discovery_includes_filtered_observations(
    tmp_path, monkeypatch, message_factory, client_factory, capsys
):
    fake = client_factory(
        [
            message_factory(1, "#F2072\nSECRET_TITLE", filename="SECRET_FILE.mp4"),
            message_factory(2, "#F2072 #F2074\nSECRET_CAPTION"),
            message_factory(3, "no tag SECRET_PHONE", filename=None, size=None, video=False),
            message_factory(4, document=False),
        ]
    )

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
                "--tag",
                "F2072",
                "--limit",
                "4",
                "--verbose",
            ]
        )
        == 0
    )
    rows = log_rows(tmp_path)
    final = event(rows, "inventory_finished")
    assert final["status"] == "limited"
    assert final["history_exhausted"] is False
    assert final["scanned_from_start"] is True
    stats = final["discovery"]
    assert stats["counts"]["videos"] == 3
    assert stats["counts"]["selected"] == 2
    assert stats["counts"]["without_tags"] == 1
    assert stats["counts"]["unknown_size"] == 1
    assert stats["counts"]["missing_filename"] == 1
    assert stats["counts"]["missing_duration"] == 1
    assert stats["counts"]["expected_bytes"] == 2468
    assert stats["repeated_tag_sample"] == [2072]
    assert stats["gap_range_sample"] == [[2073, 2073]]
    assert stats["unobserved_tag_numbers"] == 1
    assert rows[-1]["last_committed_cursor"] == 4
    assert rows[-1]["status"] == "success"
    assert len({row["run_id"] for row in rows}) == 1
    assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
    captured = capsys.readouterr()
    assert "SECRET" not in json.dumps(rows) + captured.err + captured.out


@pytest.mark.parametrize("failure", [RuntimeError("SECRET_ERROR"), asyncio.CancelledError()])
def test_failure_and_interrupt_log_only_committed_pages(
    tmp_path, message_factory, client_factory, failure
):
    fake = client_factory([])
    fake.get_messages = AsyncMock(side_effect=[[message_factory(5)], failure])
    with pytest.raises(type(failure)):
        with RunLog(tmp_path):
            with Database(tmp_path / "data/manifest.sqlite3") as db:
                asyncio.run(
                    inventory_channel(fake, "entity", CHANNEL, db, page_size=1, page_delay=0)
                )
    rows = log_rows(tmp_path)
    final = event(rows, "inventory_finished")
    assert final["discovery"]["counts"]["pages"] == 1
    assert final["cursor"] == 5
    assert final["scanned"] == 1
    assert rows[-1]["last_committed_cursor"] == 5
    interrupted = isinstance(failure, asyncio.CancelledError)
    assert rows[-1]["exit_code"] == (130 if interrupted else 1)
    assert final["status"] == ("interrupted" if interrupted else "failed")
    assert "SECRET_ERROR" not in json.dumps(rows)
    assert ACTIVE_RUN.get() is None


def test_uncommitted_page_is_not_counted(tmp_path, monkeypatch, message_factory, client_factory):
    fake = client_factory([message_factory(5)])
    with pytest.raises(OSError):
        with RunLog(tmp_path):
            with Database(tmp_path / "data/manifest.sqlite3") as db:
                monkeypatch.setattr(db, "save_page", Mock(side_effect=OSError("SECRET_PATH")))
                asyncio.run(inventory_channel(fake, "entity", CHANNEL, db, page_delay=0))
    rows = log_rows(tmp_path)
    assert not any(row["event"] == "page_committed" for row in rows)
    assert event(rows, "inventory_finished")["discovery"]["counts"] == {}
    assert rows[-1]["last_committed_cursor"] == 0


def test_retry_log_records_reason_delay_and_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_client.asyncio, "sleep", AsyncMock())
    operation = AsyncMock(side_effect=[TimeoutError("SECRET"), errors.FloodWaitError(None, 2), 1])
    with RunLog(tmp_path):
        assert asyncio.run(read_with_retry(operation, operation_name="history")) == 1
    rows = log_rows(tmp_path)
    retries = [row for row in rows if row["event"] == "read_retry"]
    assert [row["error_code"] for row in retries] == ["timeout", "flood_wait"]
    assert [row["delay_seconds"] for row in retries] == [1, 2]
    assert rows[-1]["retry_count"] == 2
    assert rows[-1]["read_calls"] == 3
    assert rows[-1]["retry_wait_seconds"] == 3
    assert "SECRET" not in json.dumps(rows)


def test_cache_miss_is_not_logged_as_failure(tmp_path):
    class Client:
        get_input_entity = AsyncMock(side_effect=ValueError("SECRET"))

        async def iter_dialogs(self):
            yield type("Dialog", (), {"id": CHANNEL, "input_entity": "target"})()

    with RunLog(tmp_path):
        assert asyncio.run(resolve_channel(Client(), CHANNEL)) == "target"
    rows = log_rows(tmp_path)
    assert event(rows, "channel_cache_miss")["level"] == "INFO"
    assert event(rows, "channel_resolved")["source"] == "dialogs"
    assert not any(row["level"] == "ERROR" for row in rows)


def test_discovery_bounds_gap_samples_and_omits_unknown_mime(message_factory):
    discovery = Discovery()
    records = [extract_video(message_factory(i, f"#F{2000 + i * 2}"), CHANNEL) for i in range(30)]
    records.append(
        extract_video(message_factory(99, "#F999999999999", mime="video/SECRET"), CHANNEL)
    )
    discovery.add(records, scanned=31, selected=31)
    stats = discovery.snapshot()
    assert len(stats["gap_range_sample"]) == 20
    assert stats["unobserved_tag_numbers"] > 100_000_000
    assert stats["mime_counts"]["other_video"] == 1
    assert "SECRET" not in json.dumps(stats)


def test_schema_error_codes_never_use_raw_exception_text():
    assert error_details(RecoveryError("SECRET", code="SECRET")) == {"error_code": "recovery_error"}
    assert error_details(errors.ChannelPrivateError(None)) == {"error_code": "channel_inaccessible"}
    assert error_details(PermissionError(13, "SECRET_PATH")) == {
        "error_code": "local_io",
        "errno": 13,
    }


def test_log_setup_failure_prevents_network(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "logs").symlink_to(outside, target_is_directory=True)
    session = Mock(side_effect=AssertionError("No network allowed"))
    monkeypatch.setattr(cli, "existing_user_session", session)
    assert cli.main(["inventory", "--root", str(tmp_path), "--channel", str(CHANNEL)]) == 1
    session.assert_not_called()
    assert list(outside.iterdir()) == []


def test_incremental_discovery_does_not_claim_full_history(
    tmp_path, message_factory, client_factory
):
    with RunLog(tmp_path):
        with Database(tmp_path / "data/manifest.sqlite3") as db:
            db.set_scan(CHANNEL, InventoryFilter().scope, 9, "limited")
            asyncio.run(
                inventory_channel(
                    client_factory([message_factory(10)]), "entity", CHANNEL, db, page_delay=0
                )
            )
    final = event(log_rows(tmp_path), "inventory_finished")
    assert final["start_cursor"] == 9
    assert final["history_exhausted"] is True
    assert final["scanned_from_start"] is False
    assert final["discovery"]["counts"]["videos"] == 1


def test_message_parse_failure_identifies_id_without_caption(
    tmp_path, message_factory, client_factory
):
    bad = message_factory(77)
    bad.document.attributes[1].duration = "SECRET_DURATION"
    with pytest.raises(ValueError):
        with RunLog(tmp_path):
            with Database(tmp_path / "data/manifest.sqlite3") as db:
                asyncio.run(
                    inventory_channel(client_factory([bad]), "entity", CHANNEL, db, page_delay=0)
                )
    rows = log_rows(tmp_path)
    assert event(rows, "message_parse_failed")["message_id"] == 77
    assert event(rows, "message_parse_failed")["error_code"] == "metadata_invalid"
    assert rows[-1]["last_committed_cursor"] == 0
    assert "SECRET_DURATION" not in json.dumps(rows)
