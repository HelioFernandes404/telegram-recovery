import asyncio
import hashlib
import json
from asyncio import sleep as real_sleep
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from telethon import errors

from telegram_recovery import cli, downloader
from telegram_recovery.database import Database
from telegram_recovery.download_files import destination, finalize
from telegram_recovery.download_plan import build_plan
from telegram_recovery.downloader import DownloadPolicy, download_items, download_one
from telegram_recovery.inventory import extract_video
from telegram_recovery.run_logging import RunLog
from telegram_recovery.security import RecoveryError

CHANNEL = -1001234567890
PAYLOAD = b"abcdefgh"
CAPTION = "#F2072 aula\n116 - Curso privado\n=001 - Modulo privado\n==005 - Aula privada"


class FakeStream:
    def __init__(self, client, events):
        self.client = client
        self.events = iter(events)
        self.closed = False
        client.active += 1
        client.max_active = max(client.active, client.max_active)

    async def __anext__(self):
        await real_sleep(0)
        event = next(self.events, None)
        if event is None:
            raise StopAsyncIteration
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self):
        if not self.closed:
            self.closed = True
            self.client.active -= 1


class FakeClient:
    def __init__(self, messages, plans=()):
        self.messages = {m.id: m for m in messages}
        self.plans = iter(plans)
        self.offsets = []
        self.reads = []
        self.active = self.max_active = 0

    async def get_messages(self, entity, *, ids):
        self.reads.append(ids)
        return self.messages.get(ids)

    def iter_download(self, document, *, offset, **kwargs):
        self.offsets.append(offset)
        return FakeStream(self, next(self.plans, [PAYLOAD[offset:]]))


def setup_case(db, message_factory, count=1):
    messages = []
    for message_id in range(1, count + 1):
        message = message_factory(message_id, CAPTION, size=len(PAYLOAD), document_id=message_id)
        message.chat_id = CHANNEL
        db.save_records([extract_video(message, CHANNEL)])
        messages.append(message)
    return messages, build_plan(db, CHANNEL)


def run_one(client, db, root, item, **kwargs):
    return asyncio.run(download_one(client, "entity", db, root, item, **kwargs))


def test_download_hash_atomic_completion_idempotence_and_private_log(
    tmp_path, message_factory, capsys
):
    with Database(tmp_path / "data/manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        db.set_scan(CHANNEL, "scope", 2200, "limited")
        scans = db.summary()["scans"]
        client = FakeClient(messages)
        with RunLog(tmp_path, command="download", verbose=True):
            assert run_one(client, db, tmp_path, items[0]) == "downloaded"
            assert run_one(client, db, tmp_path, items[0]) == "skipped"
        path = tmp_path / "downloads" / items[0].relative_path
        assert path.read_bytes() == PAYLOAD
        assert not path.with_name(path.name + ".part").exists()
        assert path.stat().st_mode & 0o777 == 0o600
        row = db.download(CHANNEL, 1)
        assert row["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
        assert row["status"] == "downloaded" and row["downloaded_at"] and row["attempts"] == 1
        assert client.offsets == [0] and db.summary()["scans"] == scans
        assert client.active == 0
    text = "".join(p.read_text() for p in (tmp_path / "logs").glob("download-*.jsonl"))
    captured = capsys.readouterr()
    assert "privado" not in text + captured.err
    assert "aula.mp4" not in text


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("SECRET"),
        ConnectionError("SECRET"),
        errors.FileReferenceExpiredError(request=None),
        errors.FloodWaitError(request=None, capture=2),
    ],
)
def test_transient_failure_resumes_offset_and_refreshes_message(
    tmp_path, message_factory, monkeypatch, failure
):
    sleep = AsyncMock()
    monkeypatch.setattr(downloader.asyncio, "sleep", sleep)
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages, [[PAYLOAD[:3], failure]])
        assert run_one(client, db, tmp_path, items[0]) == "downloaded"
        assert client.offsets == [0, 3] and client.reads == [1, 1]
        assert db.download(CHANNEL, 1)["attempts"] == 2
        assert client.active == 0
        if isinstance(failure, errors.FloodWaitError):
            sleep.assert_awaited_once_with(2)


def test_interruption_resume_and_partial_prefix_integrity(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages, [[PAYLOAD[:3], asyncio.CancelledError()]])
        with pytest.raises(asyncio.CancelledError):
            run_one(client, db, tmp_path, items[0])
        saved = db.download(CHANNEL, 1)
        assert saved["status"] == "interrupted" and saved["bytes_downloaded"] == 3
        assert saved["partial_sha256"] == hashlib.sha256(PAYLOAD[:3]).hexdigest()
        assert run_one(client, db, tmp_path, items[0]) == "downloaded"
        assert client.offsets == [0, 3]


@pytest.mark.parametrize("received", [3, len(PAYLOAD)])
def test_cancellation_racing_with_received_chunk_preserves_partial(
    tmp_path, message_factory, received
):
    class RacingStream(FakeStream):
        async def __anext__(self):
            chunk = await super().__anext__()
            if chunk == PAYLOAD[:received]:
                # A request completes in the same loop turn as Ctrl+C cancellation.
                asyncio.get_running_loop().call_soon(owner.cancel)
            return chunk

    async def transfer(client, db, item):
        nonlocal owner
        owner = asyncio.create_task(download_one(client, "entity", db, tmp_path, item))
        with pytest.raises(asyncio.CancelledError):
            await owner

    owner = None
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages)
        chunks = [PAYLOAD[:received]]
        if received < len(PAYLOAD):
            chunks.append(PAYLOAD[received:])
        client.iter_download = lambda *a, **kw: RacingStream(client, chunks)
        asyncio.run(transfer(client, db, items[0]))
        saved = db.download(CHANNEL, 1)
        assert saved["status"] == "interrupted"
        assert saved["bytes_downloaded"] in (0, received)
        final = tmp_path / "downloads" / items[0].relative_path
        assert not final.exists()
        part = final.with_name(final.name + ".part")
        assert part.read_bytes() == PAYLOAD[: saved["bytes_downloaded"]]
        assert client.active == 0
        fresh_client = FakeClient(messages)
        assert run_one(fresh_client, db, tmp_path, items[0]) == "downloaded"
        assert fresh_client.offsets == (
            [saved["bytes_downloaded"]] if saved["bytes_downloaded"] < len(PAYLOAD) else []
        )


@pytest.mark.parametrize("alteration", ["corruption", "replaced_media"])
def test_resume_rejects_changed_prefix_or_document(tmp_path, message_factory, alteration):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages, [[PAYLOAD[:3], asyncio.CancelledError()]])
        with pytest.raises(asyncio.CancelledError):
            run_one(client, db, tmp_path, items[0])
        part = tmp_path / "downloads" / (items[0].relative_path + ".part")
        if alteration == "corruption":
            part.write_bytes(b"BAD")
        else:
            messages[0].document.id = 999
        before = part.read_bytes()
        with pytest.raises(RecoveryError):
            run_one(client, db, tmp_path, items[0])
        assert part.read_bytes() == before and client.offsets == [0]


@pytest.mark.parametrize("which", ["final", "part"])
def test_foreign_existing_files_never_overwritten_or_adopted(tmp_path, message_factory, which):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        path = destination(tmp_path, items[0].relative_path, CHANNEL)
        if which == "part":
            path = path.with_name(path.name + ".part")
        path.write_bytes(PAYLOAD)
        client = FakeClient(messages)
        for _ in range(2):
            with pytest.raises(RecoveryError):
                run_one(client, db, tmp_path, items[0])
        assert path.read_bytes() == PAYLOAD and client.offsets == []


def test_atomic_publish_conflict_preserves_both_files(tmp_path):
    part, final = tmp_path / "video.part", tmp_path / "video.mp4"
    part.write_bytes(b"new")
    final.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        finalize(part, final)
    assert final.read_bytes() == b"existing" and part.read_bytes() == b"new"


def test_symlink_module_folder_is_rejected(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        parts = items[0].relative_path.split("/")
        parent = tmp_path / "downloads" / parts[0]
        parent.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (parent / parts[1]).symlink_to(outside, target_is_directory=True)
        with pytest.raises(RecoveryError):
            run_one(FakeClient(messages), db, tmp_path, items[0])
        assert list(outside.iterdir()) == []


def test_short_stream_failure_retains_bytes_and_later_run_recovers(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages, [[PAYLOAD[:2]]])
        with pytest.raises(RecoveryError):
            run_one(client, db, tmp_path, items[0], policy=DownloadPolicy(attempts=1))
        assert db.download(CHANNEL, 1)["bytes_downloaded"] == 2
        assert run_one(client, db, tmp_path, items[0]) == "downloaded"
        assert client.offsets == [0, 2]


def test_crash_after_publication_recovers_without_redownload(
    tmp_path, message_factory, monkeypatch
):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        real_finalize = downloader.finalize

        def crashing_finalize(*args):
            real_finalize(*args)
            raise OSError("Simulated post-rename failure")

        monkeypatch.setattr(downloader, "finalize", crashing_finalize)
        client = FakeClient(messages)
        with pytest.raises(OSError):
            run_one(client, db, tmp_path, items[0])
        assert run_one(client, db, tmp_path, items[0]) == "skipped"
        assert client.offsets == [0] and db.download(CHANNEL, 1)["status"] == "downloaded"


def test_concurrency_is_bounded_and_failure_does_not_erase_other_results(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory, count=5)
        client = FakeClient(messages[1:])  # Message 1 is missing.
        counts = asyncio.run(download_items(client, "entity", db, tmp_path, items, concurrency=2))
        assert counts == {
            "selected": 5,
            "downloaded": 4,
            "skipped": 0,
            "failed": 1,
            "modules_skipped": 0,
        }
        assert client.max_active <= 2 and client.active == 0
        assert db.download(CHANNEL, 1)["error_code"] == "message_missing"


def test_download_cli_uses_existing_session_and_reports_failures(
    tmp_path, message_factory, monkeypatch, capsys
):
    with Database(tmp_path / "data/manifest.sqlite3") as db:
        messages, _ = setup_case(db, message_factory, count=2)
    client = FakeClient(messages[1:])

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield client

    monkeypatch.setattr(cli, "existing_user_session", session)
    monkeypatch.setattr(cli, "resolve_channel", AsyncMock(return_value="entity"))
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
                "1",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["failed"] == 1
    assert "privado" not in captured.err
    rows = [
        json.loads(line)
        for p in (tmp_path / "logs").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert rows[-1]["status"] == "failed" and rows[-1]["error_code"] == "download_failed"
    assert not any(event["event"] == "auth_prompt" for event in rows)


def test_large_flood_wait_stops_without_retrying(tmp_path, message_factory, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(downloader.asyncio, "sleep", sleep)
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages, [[errors.FloodWaitError(request=None, capture=301)]])
        with pytest.raises(RecoveryError) as error:
            run_one(client, db, tmp_path, items[0])
        assert error.value.code == "flood_wait_limit"
        assert client.offsets == [0] and client.active == 0
        sleep.assert_not_called()


def test_oversized_response_never_publishes_final_file(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages, [[PAYLOAD + b"bad"]])
        with pytest.raises(RecoveryError) as error:
            run_one(client, db, tmp_path, items[0])
        assert error.value.code == "size_mismatch"
        assert not (tmp_path / "downloads" / items[0].relative_path).exists()
        assert db.download(CHANNEL, 1)["bytes_downloaded"] == 0


def test_completed_partial_after_interruption_only_needs_publication(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages, [[PAYLOAD, asyncio.CancelledError()]])
        with pytest.raises(asyncio.CancelledError):
            run_one(client, db, tmp_path, items[0])
        assert run_one(client, db, tmp_path, items[0]) == "downloaded"
        assert client.offsets == [0]  # No media transfer on the second execution.


def test_known_final_hash_mismatch_is_preserved(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages)
        run_one(client, db, tmp_path, items[0])
        final = tmp_path / "downloads" / items[0].relative_path
        final.write_bytes(b"x" * len(PAYLOAD))
        with pytest.raises(RecoveryError):
            run_one(client, db, tmp_path, items[0])
        assert final.read_bytes() == b"x" * len(PAYLOAD)
        assert client.offsets == [0]


def test_atomic_fallback_does_not_overwrite(tmp_path, monkeypatch):
    from telegram_recovery import download_files

    monkeypatch.setattr(download_files.ctypes, "CDLL", lambda *a, **kw: object())
    part, final = tmp_path / "a.part", tmp_path / "a.mp4"
    part.write_bytes(PAYLOAD)
    finalize(part, final)
    assert final.read_bytes() == PAYLOAD and not part.exists()
    part.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        finalize(part, final)
    assert final.read_bytes() == PAYLOAD and part.read_bytes() == b"new"


def test_chunk_timeout_cancels_stream_and_preserves_partial(tmp_path, message_factory):
    class SlowStream:
        closed = False

        async def __anext__(self):
            await real_sleep(1)
            return PAYLOAD

        async def close(self):
            self.closed = True

    with Database(tmp_path / "manifest.sqlite3") as db:
        messages, items = setup_case(db, message_factory)
        client = FakeClient(messages)
        stream = SlowStream()
        client.iter_download = lambda *a, **kw: stream
        with pytest.raises(RecoveryError):
            run_one(client, db, tmp_path, items[0], policy=DownloadPolicy(attempts=1, timeout=0.01))
        assert stream.closed
        assert db.download(CHANNEL, 1)["status"] == "failed"
