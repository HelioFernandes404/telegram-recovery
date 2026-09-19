import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_recovery import cli
from telegram_recovery.database import Database
from telegram_recovery.download_files import destination
from telegram_recovery.download_plan import build_plan
from telegram_recovery.inventory import InventoryFilter, extract_video
from telegram_recovery.module_check import check_modules

CHANNEL = -1001234567890
PAYLOAD = b"video simulation"


def add_lesson(db, factory, message_id, *, channel=CHANNEL, course=116, module=1):
    caption = (
        f"#F{message_id:04d} aula\n{course:03d} - Curso privado\n"
        f"={module:03d} - Modulo privado\n=={message_id:03d} - Aula privada"
    )
    record = extract_video(factory(message_id, caption, size=len(PAYLOAD)), channel)
    db.save_records([record])
    return record


def complete(db, root, item):
    path = destination(root, item.relative_path, item.video["channel_id"])
    path.write_bytes(PAYLOAD)
    db.add_download(item.video, item.relative_path)
    db.update_download(
        item.video["channel_id"],
        item.video["message_id"],
        status="downloaded",
        sha256=hashlib.sha256(PAYLOAD).hexdigest(),
        bytes_downloaded=len(PAYLOAD),
        downloaded_at="2026-01-01T00:00:00+00:00",
    )
    return path


def inspect(db, root, items):
    return asyncio.run(check_modules(db, root, items))


def test_repeated_module_download_finishes_offline_without_file_or_record_changes(
    tmp_path, message_factory, monkeypatch, capsys
):
    with Database(tmp_path / "data/manifest.sqlite3") as db:
        for message_id in (1, 2):
            add_lesson(db, message_factory, message_id)
        items = build_plan(db, CHANNEL)
        paths = [complete(db, tmp_path, i) for i in items]
        saved = [db.download(CHANNEL, i) for i in (1, 2)]
        db.set_scan(CHANNEL, "scope", 2200, "limited")
        scans = db.summary()["scans"]
    session = Mock(side_effect=AssertionError("Completed module must not connect"))
    monkeypatch.setattr(cli, "existing_user_session", session)
    for _ in range(2):
        assert (
            cli.main(
                [
                    "download",
                    "--root",
                    str(tmp_path),
                    "--channel",
                    str(CHANNEL),
                    "--course",
                    "116",
                    "--module",
                    "001",
                ]
            )
            == 0
        )
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == {
            "selected": 2,
            "downloaded": 0,
            "skipped": 2,
            "failed": 0,
            "modules_skipped": 1,
        }
        assert "já completo" in captured.err and "privado" not in captured.err
    session.assert_not_called()
    with Database(tmp_path / "data/manifest.sqlite3", readonly=True) as db:
        assert [db.download(CHANNEL, i) for i in (1, 2)] == saved
        assert db.summary()["scans"] == scans
    assert [p.read_bytes() for p in paths] == [PAYLOAD, PAYLOAD]
    assert len(list((tmp_path / "downloads").rglob("*.mp4"))) == 2
    logs = [
        json.loads(line)
        for p in (tmp_path / "logs").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert sum(e["event"] == "module_checked" and e["complete"] for e in logs) == 2
    assert "privado" not in json.dumps(logs)


def test_incomplete_module_sends_only_missing_lesson_to_transfer(
    tmp_path, message_factory, monkeypatch, capsys
):
    with Database(tmp_path / "data/manifest.sqlite3") as db:
        for message_id in (1, 2):
            add_lesson(db, message_factory, message_id)
        items = build_plan(db, CHANNEL)
        complete(db, tmp_path, items[0])
    fake_client = object()

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield fake_client

    async def transfer(client, entity, db, root, pending, **kwargs):
        assert client is fake_client
        assert [i.video["message_id"] for i in pending] == [2]
        assert kwargs["already_skipped"] == 1 and kwargs["modules_skipped"] == 0
        return {"selected": 2, "downloaded": 1, "skipped": 1, "failed": 0, "modules_skipped": 0}

    monkeypatch.setattr(cli, "existing_user_session", session)
    monkeypatch.setattr(cli, "resolve_channel", AsyncMock(return_value="entity"))
    monkeypatch.setattr(cli, "download_items", transfer)
    assert cli.main(["download", "--root", str(tmp_path), "--channel", str(CHANNEL)]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == 1


@pytest.mark.parametrize("selection", ["limit", "tag", "message"])
def test_filtered_selection_never_marks_an_incomplete_module_complete(
    tmp_path, message_factory, selection
):
    with Database(tmp_path / "manifest.sqlite3") as db:
        for message_id in (1, 2):
            add_lesson(db, message_factory, message_id)
        complete(db, tmp_path, build_plan(db, CHANNEL)[0])
        options = {
            "limit": {"limit": 1},
            "tag": {"filters": InventoryFilter(tags=("F0001",))},
            "message": {"message_id": 1},
        }[selection]
        checked = inspect(db, tmp_path, build_plan(db, CHANNEL, **options))
        assert len(checked.completed) == 1 and checked.pending == []
        assert checked.completed_modules == 0
        assert checked.modules[0]["known_videos"] == 2
        assert checked.modules[0]["selected_videos"] == 1


@pytest.mark.parametrize(
    "change", ["new_lesson", "document", "size", "deleted", "corrupted", "symlink"]
)
def test_module_completion_is_rechecked_after_inventory_or_file_changes(
    tmp_path, message_factory, change
):
    with Database(tmp_path / "manifest.sqlite3") as db:
        record = add_lesson(db, message_factory, 1)
        path = complete(db, tmp_path, build_plan(db, CHANNEL)[0])
        assert inspect(db, tmp_path, build_plan(db, CHANNEL)).completed_modules == 1
        if change == "new_lesson":
            add_lesson(db, message_factory, 2)
        elif change == "document":
            db.save_records([replace(record, document_id=record.document_id + 1)])
        elif change == "size":
            db.save_records([replace(record, expected_size=len(PAYLOAD) + 1)])
        elif change == "deleted":
            path.unlink()  # Simulated external deletion, never done by the checker.
        elif change == "corrupted":
            path.write_bytes(b"x" * len(PAYLOAD))
        else:
            path.unlink()
            outside = tmp_path / "outside.mp4"
            outside.write_bytes(PAYLOAD)
            path.symlink_to(outside)
        checked = inspect(db, tmp_path, build_plan(db, CHANNEL))
        assert checked.completed_modules == 0
        assert len(checked.pending) == 1


def test_matching_module_numbers_in_different_courses_or_channels_are_independent(
    tmp_path, message_factory
):
    with Database(tmp_path / "manifest.sqlite3") as db:
        add_lesson(db, message_factory, 1)
        add_lesson(db, message_factory, 2, course=117)
        add_lesson(db, message_factory, 1, channel=CHANNEL - 1)
        items = build_plan(db, CHANNEL)
        complete(db, tmp_path, items[0])
        selected = items + build_plan(db, CHANNEL - 1)
        checked = inspect(db, tmp_path, selected)
        assert checked.completed_modules == 1 and len(checked.pending) == 2
        assert len(checked.modules) == 3


def test_preview_checks_existing_hashes_without_database_writes_or_network(
    tmp_path, message_factory, monkeypatch, capsys
):
    path = tmp_path / "data/manifest.sqlite3"
    with Database(path) as db:
        add_lesson(db, message_factory, 1)
        complete(db, tmp_path, build_plan(db, CHANNEL)[0])
    before = path.read_bytes()
    monkeypatch.setattr(
        cli, "existing_user_session", Mock(side_effect=AssertionError("No network"))
    )
    assert (
        cli.main(["download", "--root", str(tmp_path), "--channel", str(CHANNEL), "--dry-run"]) == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["already_downloaded"] == 1 and preview["pending_videos"] == 0
    assert preview["modules_complete"] == 1 and preview["pending_expected_bytes"] == 0
    assert path.read_bytes() == before and not (tmp_path / "logs").exists()


def test_unclassified_video_is_skipped_without_claiming_a_complete_module(
    tmp_path, message_factory
):
    with Database(tmp_path / "manifest.sqlite3") as db:
        db.save_records(
            [extract_video(message_factory(1, "Sem módulo", size=len(PAYLOAD)), CHANNEL)]
        )
        items = build_plan(db, CHANNEL)
        complete(db, tmp_path, items[0])
        checked = inspect(db, tmp_path, items)
        assert len(checked.completed) == 1 and checked.completed_modules == 0
