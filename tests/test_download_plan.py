import json
import sqlite3
from dataclasses import replace
from unittest.mock import Mock

import pytest

from telegram_recovery import cli
from telegram_recovery.database import SCHEMA, Database
from telegram_recovery.download_plan import build_plan, plan_summary
from telegram_recovery.inventory import InventoryFilter, extract_video

CHANNEL = -1001234567890


def caption(course=116, module=1, lesson=5, title="Redes"):
    return f"#F2072 aula\n{course:03} - {title}\n={module:03} - Protocolos\n=={lesson:03} - Aula"


def test_layout_groups_modules_selects_intersection_and_keeps_untagged(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        db.save_records(
            [
                extract_video(message_factory(1, caption()), CHANNEL),
                extract_video(message_factory(2, caption(module=2)), CHANNEL),
                extract_video(message_factory(3, caption(course=117)), CHANNEL),
                extract_video(message_factory(4, "Sem tag"), CHANNEL),
                extract_video(
                    message_factory(5, caption(lesson=6, title="Redes renomeado")), CHANNEL
                ),
            ]
        )
        items = build_plan(db, CHANNEL, course=116, module=1)
        assert [i.video["message_id"] for i in items] == [1, 5]
        for item in items:
            assert item.relative_path.startswith(f"channel_{CHANNEL}/116_Redes/001_Protocolos/")
        assert len({i.relative_path for i in items}) == 2
        assert len(build_plan(db, CHANNEL, course=116, module=1, limit=1)) == 1
        assert len(build_plan(db, CHANNEL, message_id=2)) == 1
        assert build_plan(db, CHANNEL, course=116, filters=InventoryFilter(tags=("F9999",))) == []
        all_items = build_plan(db, CHANNEL)
        assert all_items[-1].relative_path.startswith(f"channel_{CHANNEL}/SEM_CURSO/SEM_MODULO/")
        assert len(plan_summary(all_items)["modules"]) == 4
        assert plan_summary(all_items)["expected_bytes"] == 5 * 1234
        with pytest.raises(ValueError):
            build_plan(db, CHANNEL, module=1)


def test_existing_destination_is_stable_after_caption_edit(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        record = extract_video(message_factory(1, caption()), CHANNEL)
        db.save_records([record])
        original = build_plan(db, CHANNEL)[0]
        db.add_download(original.video, original.relative_path)
        db.save_records([replace(record, caption=caption(title="Novo nome"))])
        assert build_plan(db, CHANNEL)[0].relative_path == original.relative_path


def test_readonly_v1_preview_does_not_migrate_and_v2_preserves_inventory(tmp_path, message_factory):
    path = tmp_path / "data" / "manifest.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as old:
        old.executescript(
            SCHEMA.split("CREATE TABLE IF NOT EXISTS downloads")[0] + "PRAGMA user_version = 1;"
        )
        old.execute(
            "INSERT INTO inventory_scans VALUES (?, 'scope', 2200, 'limited', 'original')",
            (CHANNEL,),
        )
    before = path.read_bytes()
    with Database(path, readonly=True) as db:
        assert db.summary()["scans"][0]["last_message_id"] == 2200
        assert build_plan(db, CHANNEL) == []
    assert path.read_bytes() == before
    with Database(path) as db:
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.summary()["scans"][0]["updated_at"] == "original"
        assert db.download(CHANNEL, 1) is None


def test_cli_preview_is_offline_and_leaves_files_unchanged(
    tmp_path, message_factory, monkeypatch, capsys
):
    path = tmp_path / "data" / "manifest.sqlite3"
    with Database(path) as db:
        db.save_records([extract_video(message_factory(1, caption()), CHANNEL)])
    before = path.read_bytes()
    session = Mock(side_effect=AssertionError("Dry run must not open Telegram"))
    monkeypatch.setattr(cli, "existing_user_session", session)
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
                "--dry-run",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["selected"] == 1 and preview["dry_run"] is True
    assert path.read_bytes() == before
    assert not (tmp_path / "downloads").exists() and not (tmp_path / "logs").exists()
    session.assert_not_called()


@pytest.mark.parametrize("flags", [["--module", "1"], ["--concurrency", "9"], ["--course", "../1"]])
def test_cli_rejects_ambiguous_or_invalid_selection_before_access(flags, monkeypatch):
    session = Mock(side_effect=AssertionError("No network"))
    monkeypatch.setattr(cli, "existing_user_session", session)
    with pytest.raises(SystemExit) as error:
        cli.main(["download", "--channel", str(CHANNEL), *flags])
    assert error.value.code == 2
    session.assert_not_called()


def test_dotted_channel_layout_filters_and_preserves_other_channel(tmp_path, message_factory):
    import asyncio

    from telegram_recovery.module_check import check_modules

    channel = -1001234567891
    with Database(tmp_path / "manifest.sqlite3") as db:
        db.save_records(
            [
                extract_video(
                    message_factory(2, "#F001 1. Aula\n1. Bem vindo\n=1. Introducao"), channel
                ),
                extract_video(
                    message_factory(3, "#F002 1. Aula\n2. Fundamentos\n=1. O que e DevOps"), channel
                ),
                extract_video(
                    message_factory(4, "#F003 1. Aula\n2. Fundamentos\n=2. Praticas"), channel
                ),
                extract_video(message_factory(3, caption()), CHANNEL),
            ]
        )
        selected = build_plan(db, channel, course=0, module=2)
        assert len(selected) == 2
        assert all(
            i.relative_path.startswith(f"channel_{channel}/000_Curso_do_canal/002_Fundamentos/")
            for i in selected
        )
        assert (
            build_plan(db, channel, filters=InventoryFilter(tags=("F003",)))[0].video["message_id"]
            == 4
        )
        checked = asyncio.run(check_modules(db, tmp_path, selected))
        assert checked.modules[0]["known_videos"] == 2 and checked.completed_modules == 0
        assert build_plan(db, CHANNEL)[0].course == 116
