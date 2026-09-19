import asyncio
import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from telegram_recovery import audited_batch as batch
from telegram_recovery.database import Database
from telegram_recovery.download_files import destination
from telegram_recovery.download_plan import build_plan
from telegram_recovery.inventory import extract_video
from telegram_recovery.security import RecoveryError, exclusive_lock

CHANNEL = -1001234567890
PAYLOAD = b"abcdefgh"


def setup(tmp_path, message_factory, count=3):
    with Database(tmp_path / "data/manifest.sqlite3") as db:
        for mid in range(1, count + 1):
            message = message_factory(
                mid,
                f"#F{mid:04}\n{mid:03} - Curso\n=001 - Modulo\n==001 - Aula",
                size=len(PAYLOAD),
                document_id=mid,
            )
            db.save_records([extract_video(message, CHANNEL)])
    return batch.prepare(tmp_path, "test", CHANNEL, [])


def publish(db, root, item):
    path = destination(root, item.relative_path, CHANNEL)
    path.write_bytes(PAYLOAD)
    db.add_download(item.video, item.relative_path)
    db.update_download(
        CHANNEL,
        item.video["message_id"],
        status="downloaded",
        bytes_downloaded=len(PAYLOAD),
        sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    )
    return path


def status(tmp_path):
    return json.loads((tmp_path / "data/batches/test/status.json").read_text())


def mock_probe(monkeypatch):
    monkeypatch.setattr(batch.shutil, "which", lambda _: "/fake/ffprobe")
    probe = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=b'{"streams":[{"codec_type":"video"}]}', stderr=b""
        )
    )
    monkeypatch.setattr(batch.subprocess, "run", probe)
    return probe


def fake_network(monkeypatch, transfer):
    @asynccontextmanager
    async def session(*args, **kwargs):
        yield "client"

    async def resolve(*args, **kwargs):
        return "entity"

    monkeypatch.setattr(batch, "existing_user_session", session)
    monkeypatch.setattr(batch, "resolve_channel", resolve)
    monkeypatch.setattr(batch, "download_one", transfer)


def test_frozen_plan_excludes_courses_and_cannot_be_overwritten(tmp_path, message_factory):
    setup(tmp_path, message_factory)
    plan = batch.prepare(tmp_path, "filtered", CHANNEL, [1, 3])
    assert [i["course"] for i in plan["items"]] == [2]
    with pytest.raises(FileExistsError):
        batch.prepare(tmp_path, "filtered", CHANNEL, [])
    assert json.loads((tmp_path / "data/batches/filtered/plan.json").read_text()) == plan
    assert not (tmp_path / "sessions").exists()


@pytest.mark.parametrize("mutation", ["size", "document", "duplicate", "path", "empty"])
def test_changed_or_ambiguous_plan_rejected(tmp_path, message_factory, mutation):
    plan = setup(tmp_path, message_factory)
    if mutation == "size":
        plan["items"][0]["expected_size"] += 1
    elif mutation == "document":
        plan["items"][0]["document_id"] += 1
    elif mutation == "duplicate":
        plan["items"].append(plan["items"][0])
    elif mutation == "path":
        plan["items"][0]["relative_path"] = "../outside"
    else:
        plan["items"] = []
    with Database(tmp_path / "data/manifest.sqlite3", readonly=True) as db:
        with pytest.raises(RecoveryError, match="Plano"):
            batch.load_selection(db, plan)


@pytest.mark.parametrize(
    "problem,code",
    [
        ("missing", "size_mismatch"),
        ("size", "size_mismatch"),
        ("hash", "checksum_mismatch"),
        ("part", "file_conflict"),
        ("symlink", "file_conflict"),
        ("probe", "audit_failed"),
        ("media", "media_changed"),
        ("changed_during_probe", "file_conflict"),
    ],
)
def test_audit_detects_conflicts_without_modifying_files(
    tmp_path, message_factory, monkeypatch, problem, code
):
    setup(tmp_path, message_factory, count=1)
    probe = mock_probe(monkeypatch)
    with Database(tmp_path / "data/manifest.sqlite3") as db:
        item = build_plan(db, CHANNEL)[0]
        path = publish(db, tmp_path, item)
        saved = db.download(CHANNEL, 1)
        if problem == "missing":
            path.unlink()
        elif problem == "size":
            path.write_bytes(b"short")
        elif problem == "hash":
            path.write_bytes(b"ABCDEFGH")
        elif problem == "part":
            path.with_name(path.name + ".part").write_bytes(b"x")
        elif problem == "symlink":
            target = tmp_path / "original"
            path.rename(target)
            path.symlink_to(target)
        elif problem == "probe":
            probe.return_value = SimpleNamespace(returncode=1, stdout=b"", stderr=b"SECRET")
        elif problem == "media":
            saved["document_id"] += 1
        else:

            def change(*args, **kwargs):
                path.write_bytes(b"ABCDEFGH")
                return SimpleNamespace(
                    returncode=0, stdout=b'{"streams":[{"codec_type":"video"}]}', stderr=b""
                )

            probe.side_effect = change
        result = batch.audit_file(tmp_path, item, saved, "/fake/ffprobe")
        assert not result["ok"] and result["error_code"] == code
        assert "SECRET" not in json.dumps(result)


def test_completed_batch_is_offline_rechecked_and_reports_duplicates(
    tmp_path, message_factory, monkeypatch
):
    setup(tmp_path, message_factory)
    probe = mock_probe(monkeypatch)
    with Database(tmp_path / "data/manifest.sqlite3") as db:
        paths = [publish(db, tmp_path, i) for i in build_plan(db, CHANNEL)]
    before = [batch.signature(p) for p in paths]
    session = Mock(side_effect=AssertionError("Must remain offline"))
    monkeypatch.setattr(batch, "existing_user_session", session)
    for _ in range(2):
        asyncio.run(batch.run(tmp_path, "test", 3))
        report = status(tmp_path)
        assert report["state"] == "completed" and report["verified"] == 3
        assert report["duplicates"] == 2 and report["pending"] == 0
    assert [batch.signature(p) for p in paths] == before
    assert probe.call_count == 6
    session.assert_not_called()
    for path in (tmp_path / "data/batches/test").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_workers_are_bounded_and_every_file_is_audited(tmp_path, message_factory, monkeypatch):
    setup(tmp_path, message_factory, count=7)
    probe = mock_probe(monkeypatch)
    active = maximum = 0

    async def transfer(client, entity, db, root, item, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        publish(db, root, item)
        active -= 1

    fake_network(monkeypatch, transfer)
    asyncio.run(batch.run(tmp_path, "test", 3))
    assert maximum == 3 and active == 0
    assert status(tmp_path)["verified"] == 7 and probe.call_count == 7


def test_audit_failure_cancels_siblings_and_stops_queue(tmp_path, message_factory, monkeypatch):
    setup(tmp_path, message_factory, count=7)
    mock_probe(monkeypatch)
    started, cancelled = [], []

    async def transfer(client, entity, db, root, item, **kwargs):
        mid = item.video["message_id"]
        started.append(mid)
        if mid == 1:
            path = publish(db, root, item)
            path.write_bytes(b"bad")
        else:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.append(mid)
                raise

    fake_network(monkeypatch, transfer)
    assert batch.main(["--root", str(tmp_path), "run", "--job", "test", "--concurrency", "3"]) == 1
    report = status(tmp_path)
    assert report["state"] == "needs_attention" and report["error_code"] == "size_mismatch"
    assert report["verified"] == 0 and report["failed_message_id"] == 1
    assert sorted(started) == [1, 2, 3] and sorted(cancelled) == [2, 3]
    with sqlite3.connect(tmp_path / "data/batches/test/audit.sqlite3") as db:
        assert db.execute("SELECT ok FROM audits").fetchone() == (0,)


def test_interruption_status_and_resume_without_redownloading(
    tmp_path, message_factory, monkeypatch
):
    setup(tmp_path, message_factory, count=2)
    mock_probe(monkeypatch)
    interrupted = asyncio.Event()

    async def transfer(client, entity, db, root, item, **kwargs):
        if item.video["message_id"] == 1:
            publish(db, root, item)
        else:
            interrupted.set()
            await asyncio.Future()

    fake_network(monkeypatch, transfer)

    async def cancel_run():
        task = asyncio.create_task(batch.run(tmp_path, "test", 1))
        await interrupted.wait()
        task.cancel()
        with pytest.raises(RecoveryError) as error:
            await task
        assert error.value.code == "interrupted"

    asyncio.run(cancel_run())
    assert status(tmp_path)["state"] == "interrupted"
    assert status(tmp_path)["verified"] == 1
    resumed = []

    async def resume(client, entity, db, root, item, **kwargs):
        resumed.append(item.video["message_id"])
        publish(db, root, item)

    fake_network(monkeypatch, resume)
    asyncio.run(batch.run(tmp_path, "test", 1))
    assert resumed == [2] and status(tmp_path)["verified"] == 2


def test_failure_logs_are_sanitized_and_plan_is_bound_to_audit(
    tmp_path, message_factory, monkeypatch, capsys
):
    setup(tmp_path, message_factory, count=1)

    async def transfer(*args, **kwargs):
        raise RuntimeError("SECRET api_hash phone password")

    fake_network(monkeypatch, transfer)
    assert batch.main(["--root", str(tmp_path), "run", "--job", "test"]) == 1
    captured = capsys.readouterr()
    text = "".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    assert "SECRET" not in captured.out + captured.err + text
    assert status(tmp_path)["error_code"] == "runtime_error"
    with pytest.raises(RecoveryError):
        batch.AuditStore(tmp_path / "data/batches/test/audit.sqlite3", "different-plan")


def test_second_runner_cannot_replace_live_status(tmp_path, message_factory):
    setup(tmp_path, message_factory, count=1)
    target = tmp_path / "data/batches/test/status.json"
    batch.atomic_json(target, {"state": "downloading"})
    before = target.read_bytes()
    with exclusive_lock(tmp_path / "data/.inventory.lock"):
        assert batch.main(["--root", str(tmp_path), "run", "--job", "test"]) == 1
    assert target.read_bytes() == before
