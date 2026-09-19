import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    Message,
    MessageMediaDocument,
    PeerChannel,
)

from telegram_recovery.database import Database
from telegram_recovery.inventory import InventoryFilter, extract_video, inventory_channel
from telegram_recovery.security import RecoveryError
from telegram_recovery.telegram_client import RetryPolicy

CHANNEL = -1001234567890


def run(client, db, **kwargs):
    return asyncio.run(inventory_channel(client, "entity", CHANNEL, db, page_delay=0, **kwargs))


def test_actual_telethon_message_types(message_factory):
    # Construct real TL objects locally to exercise Message.document and attributes.
    date = message_factory().date
    document = Document(
        id=123,
        access_hash=0,
        file_reference=b"",
        date=date,
        mime_type="video/mp4",
        size=2048,
        dc_id=1,
        attributes=[
            DocumentAttributeFilename("aula.mp4"),
            DocumentAttributeVideo(duration=65, w=1280, h=720),
        ],
    )
    message = Message(
        id=50,
        peer_id=PeerChannel(2228510001),
        date=date,
        message="#F2072\nAula realista",
        media=MessageMediaDocument(document=document),
    )
    record = extract_video(message, CHANNEL)
    assert (record.document_id, record.duration, record.expected_size) == (123, 65, 2048)
    assert record.tags == ["F2072"]


def test_extract_all_fields(message_factory):
    record = extract_video(message_factory(caption="#f2072 #F2073\nCamadas de Rede"), CHANNEL)
    assert record.channel_id == CHANNEL
    assert record.message_id == 1
    assert record.date == "2024-01-01T00:00:00+00:00"
    assert record.caption == "#f2072 #F2073\nCamadas de Rede"
    assert record.tags == ["F2072", "F2073"]
    assert record.title == "Camadas de Rede"
    assert record.original_filename == "aula.mp4"
    assert record.mime_type == "video/mp4"
    assert record.expected_size == 1234
    assert record.duration == 42.5
    assert record.document_id == 77


@pytest.mark.parametrize(
    "mime,filename,video,expected",
    [
        ("video/webm", None, False, True),
        ("application/octet-stream", "AULA.MKV", False, True),
        ("application/octet-stream", None, True, True),
        ("application/pdf", "aula.pdf", False, False),
        (None, None, False, False),
    ],
)
def test_video_detection(message_factory, mime, filename, video, expected):
    message = message_factory(mime=mime, filename=filename, video=video)
    assert (extract_video(message, CHANNEL) is not None) == expected


def test_no_document_and_missing_metadata(message_factory):
    assert extract_video(message_factory(document=False), CHANNEL) is None
    message = message_factory(caption=None, filename=None, size=None, video=False)
    message.date = None
    record = extract_video(message, CHANNEL)
    assert record.tags == []
    assert record.title == "Mensagem 1"
    assert record.expected_size is None
    assert record.duration is None
    assert record.date is None


def test_pagination_gaps_no_tags_and_idempotence(tmp_path, message_factory, client_factory):
    client = client_factory(
        [
            message_factory(8, document=False),
            message_factory(1),
            message_factory(4, caption=""),
            message_factory(12, caption="#F2073\nOutra aula"),
        ]
    )
    with Database(tmp_path / "manifest.sqlite3") as db:
        first = run(client, db, page_size=2)
        assert (first.scanned, first.inserted, first.without_tags, first.status) == (
            4,
            3,
            1,
            "idle",
        )
        assert [call["min_id"] for call in client.calls] == [0, 4, 12]
        assert len(list(db.videos())) == 3
        assert run(client, db).scanned == 0
        again = run(client, db, rescan=True)
        assert (again.inserted, again.updated) == (0, 3)
        assert len(list(db.videos())) == 3
        client.messages.append(message_factory(20))
        assert run(client, db).inserted == 1


def test_limit_counts_all_messages_and_resumes_after_reopen(
    tmp_path, message_factory, client_factory
):
    path = tmp_path / "manifest.sqlite3"
    client = client_factory(
        [
            message_factory(1, document=False),
            message_factory(2),
            message_factory(3),
            message_factory(4),
        ]
    )
    with Database(path) as db:
        first = run(client, db, limit=2, page_size=100)
        assert (first.scanned, first.inserted, first.status) == (2, 1, "limited")
    with Database(path) as db:
        second = run(client, db, limit=3, page_size=1)
        assert (second.scanned, second.inserted, second.status) == (2, 2, "idle")
        assert len(list(db.videos())) == 3


def test_filters_have_independent_checkpoints(tmp_path, message_factory, client_factory):
    client = client_factory(
        [message_factory(1), message_factory(2, "sem tag"), message_factory(3, "#F2073")]
    )
    with Database(tmp_path / "manifest.sqlite3") as db:
        filtered = run(client, db, filters=InventoryFilter(tags=("#f2072",)))
        assert (filtered.scanned, filtered.matched) == (3, 1)
        assert len(list(db.videos())) == 1
        complete = run(client, db)
        assert (complete.scanned, complete.inserted, complete.updated) == (3, 2, 1)
        assert len(db.summary()["scans"]) == 2


@pytest.mark.parametrize(
    "filters,tags,expected",
    [
        (InventoryFilter(), [], True),
        (InventoryFilter(tags=("F2072",)), [], False),
        (InventoryFilter(tags=("F2072", "F2073")), ["F2073"], True),
        (InventoryFilter(from_tag="F9999", to_tag="F10001"), ["F10000"], True),
        (InventoryFilter(from_tag="F2072", to_tag="F2073"), ["F2072"], True),
        (InventoryFilter(from_tag="F2072", to_tag="F2073"), ["F2073"], True),
        (InventoryFilter(from_tag="F2072", to_tag="F2073"), ["F2074"], False),
        (InventoryFilter(tags=("F2072",), from_tag="F2073"), ["F2072", "F2073"], False),
    ],
)
def test_filter_semantics(filters, tags, expected):
    assert filters.matches(tags) is expected


def test_filter_scope_normalization_and_bad_range():
    assert (
        InventoryFilter(tags=("#f2073", "F2072", "F2073")).scope
        == InventoryFilter(tags=("F2072", "F2073")).scope
    )
    with pytest.raises(ValueError):
        InventoryFilter(from_tag="F2073", to_tag="F2072")
    with pytest.raises(ValueError):
        InventoryFilter(tags=("garbage",))


def test_failure_keeps_committed_page(tmp_path, message_factory, client_factory):
    client = client_factory([message_factory(1), message_factory(2)])
    read = client.get_messages
    client.get_messages = AsyncMock(side_effect=[[message_factory(1)], RuntimeError("secret")])
    with Database(tmp_path / "manifest.sqlite3") as db:
        with pytest.raises(RuntimeError):
            run(client, db, page_size=1)
        assert db.cursor(CHANNEL, InventoryFilter().scope) == 1
        assert db.summary()["scans"][0]["status"] == "failed"
        client.get_messages = read
        assert run(client, db).inserted == 1


def test_cancellation_keeps_checkpoint(tmp_path, message_factory, client_factory):
    client = client_factory([])
    client.get_messages = AsyncMock(side_effect=[[message_factory(1)], asyncio.CancelledError()])
    with Database(tmp_path / "manifest.sqlite3") as db:
        with pytest.raises(asyncio.CancelledError):
            run(client, db, page_size=1)
        assert db.cursor(CHANNEL, InventoryFilter().scope) == 1
        assert db.summary()["scans"][0]["status"] == "interrupted"


def test_page_and_cursor_roll_back_together(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        db.set_scan(CHANNEL, "scope", 3, "running")
        good = extract_video(message_factory(4), CHANNEL)
        invalid = replace(good, message_id=5, caption=None)
        with pytest.raises(sqlite3.IntegrityError):
            db.save_page([good, invalid], CHANNEL, "scope", 5)
        assert list(db.videos()) == []
        assert db.cursor(CHANNEL, "scope") == 3


def test_changed_metadata_and_channel_isolation(tmp_path, message_factory):
    with Database(tmp_path / "manifest.sqlite3") as db:
        record = extract_video(message_factory(), CHANNEL)
        db.save_page([record, replace(record, channel_id=CHANNEL - 1)], CHANNEL, "scope", 1)
        original = list(db.videos(CHANNEL))[0]
        db.save_page([replace(record, title="Edited")], CHANNEL, "scope", 1)
        edited = list(db.videos(CHANNEL))[0]
        assert edited["title"] == "Edited"
        assert edited["status"] == "inventoried"
        assert edited["first_seen_at"] == original["first_seen_at"]
        db.save_page([replace(record, document_id=78)], CHANNEL, "scope", 1)
        assert list(db.videos(CHANNEL))[0]["status"] == "media_changed"
        assert len(list(db.videos())) == 2


def test_unexpected_pagination_does_not_advance(tmp_path, message_factory, client_factory):
    client = client_factory([])
    client.get_messages = AsyncMock(return_value=[message_factory(2), message_factory(1)])
    with Database(tmp_path / "manifest.sqlite3") as db:
        with pytest.raises(RecoveryError, match="Paginação"):
            run(client, db)
        assert db.cursor(CHANNEL, InventoryFilter().scope) == 0
        assert list(db.videos()) == []


def test_network_retry_exhaustion_preserves_page(tmp_path, message_factory, client_factory):
    client = client_factory([])
    client.get_messages = AsyncMock(side_effect=[[message_factory(1)], TimeoutError("secret")])
    with Database(tmp_path / "manifest.sqlite3") as db:
        with pytest.raises(RecoveryError, match="timeout"):
            run(client, db, page_size=1, retry_policy=RetryPolicy(attempts=1))
        assert db.cursor(CHANNEL, InventoryFilter().scope) == 1
