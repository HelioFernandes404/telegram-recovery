"""Paginated, incremental metadata inventory without media downloads."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import PurePosixPath

from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

from .database import Database, VideoRecord
from .discovery import Discovery
from .naming import (
    VIDEO_EXTENSIONS,
    extract_tags,
    lesson_title,
    normalize_tag,
    original_basename,
    suggested_filename,
)
from .run_logging import emit
from .security import RecoveryError
from .telegram_client import RetryPolicy, read_with_retry


@dataclass(frozen=True)
class InventoryFilter:
    tags: tuple[str, ...] = ()
    from_tag: str | None = None
    to_tag: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "tags", tuple(sorted({normalize_tag(t) for t in self.tags})))
        for name in ("from_tag", "to_tag"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, normalize_tag(getattr(self, name)))
        if self.from_tag and self.to_tag and int(self.from_tag[1:]) > int(self.to_tag[1:]):
            raise ValueError("--from-tag deve ser menor ou igual a --to-tag.")

    @property
    def scope(self) -> str:
        # Limit and page size do not affect the scope: increasing a limit resumes the scan.
        return json.dumps(
            {"tags": self.tags, "from_tag": self.from_tag, "to_tag": self.to_tag}, sort_keys=True
        )

    def matches(self, tags: list[str]) -> bool:
        if not (self.tags or self.from_tag or self.to_tag):
            return True
        for tag in tags:
            number = int(tag[1:])
            if self.tags and tag not in self.tags:
                continue
            if self.from_tag and number < int(self.from_tag[1:]):
                continue
            if self.to_tag and number > int(self.to_tag[1:]):
                continue
            return True
        return False


def extract_video(message, channel_id: int) -> VideoRecord | None:
    document = getattr(message, "document", None)
    if document is None:
        return None
    attributes = getattr(document, "attributes", []) or []
    filename = next(
        (a.file_name for a in attributes if isinstance(a, DocumentAttributeFilename)), None
    )
    video = next((a for a in attributes if isinstance(a, DocumentAttributeVideo)), None)
    mime = getattr(document, "mime_type", None)
    extension = PurePosixPath(original_basename(filename or "")).suffix.lower()
    if not (
        video is not None
        or (mime or "").lower().startswith("video/")
        or extension in VIDEO_EXTENSIONS
    ):
        return None
    caption = getattr(message, "message", None) or ""
    tags = extract_tags(caption)
    title = lesson_title(caption, filename, message.id)
    date = getattr(message, "date", None)
    if date is not None:
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        date = date.astimezone(UTC).isoformat()
    return VideoRecord(
        channel_id=channel_id,
        message_id=message.id,
        date=date,
        caption=caption,
        tags=tags,
        title=title,
        original_filename=filename,
        mime_type=mime,
        expected_size=getattr(document, "size", None),
        duration=float(video.duration) if video is not None else None,
        document_id=document.id,
        suggested_filename=suggested_filename(tags, title, filename, message.id, caption=caption),
    )


@dataclass
class InventorySummary:
    scanned: int = 0
    videos_seen: int = 0
    matched: int = 0
    inserted: int = 0
    updated: int = 0
    without_tags: int = 0
    last_message_id: int = 0
    status: str = "running"


@dataclass(frozen=True)
class MessageInventorySummary:
    message_id: int
    outcome: str
    inserted: int = 0
    updated: int = 0


async def inventory_message(
    client,
    entity,
    channel_id: int,
    database: Database,
    message_id: int,
    *,
    retry_policy: RetryPolicy | None = None,
    progress=lambda _: None,
) -> MessageInventorySummary:
    """Read one exact message; never use its ID to advance the history checkpoint."""
    if message_id < 1:
        raise ValueError("--message-id deve ser positivo.")
    emit("message_requested", message_id=message_id)
    progress(f"Consultando metadados da mensagem {message_id}.")
    message = await read_with_retry(
        lambda: client.get_messages(entity, ids=message_id),
        policy=retry_policy,
        progress=progress,
        operation_name="message",
    )
    if message is None:
        result = MessageInventorySummary(message_id, "missing")
    else:
        if message.id != message_id or message.chat_id != channel_id:
            raise RecoveryError(
                "Resposta individual não corresponde ao canal/mensagem; manifesto preservado.",
                code="message_mismatch",
            )
        try:
            record = extract_video(message, channel_id)
        except Exception:
            emit("message_parse_failed", message_id=message_id, error_code="metadata_invalid")
            raise
        if record is None:
            result = MessageInventorySummary(message_id, "non_video")
        else:
            inserted, updated = database.save_records([record])
            result = MessageInventorySummary(message_id, "video", inserted, updated)
    emit(
        "message_inventory_finished",
        message_id=message_id,
        outcome=result.outcome,
        inserted=result.inserted,
        updated=result.updated,
    )
    return result


async def inventory_channel(
    client,
    entity,
    channel_id: int,
    database: Database,
    *,
    filters: InventoryFilter | None = None,
    limit: int | None = None,
    page_size: int = 100,
    rescan: bool = False,
    page_delay: float = 1,
    retry_policy: RetryPolicy | None = None,
    progress=lambda _: None,
) -> InventorySummary:
    if limit is not None and limit < 1:
        raise ValueError("--limit deve ser positivo.")
    if not 1 <= page_size <= 100:
        raise ValueError("--page-size deve estar entre 1 e 100.")
    filters = filters or InventoryFilter()
    scope = filters.scope
    cursor = 0 if rescan else database.cursor(channel_id, scope)
    start_cursor = cursor
    started = time.monotonic()
    discovery = Discovery()
    result = InventorySummary(last_message_id=cursor)
    database.set_scan(channel_id, scope, cursor, "running")
    progress(f"Inventário iniciado; cursor={cursor}, página={page_size}.")
    emit("inventory_started", cursor=cursor, rescan=rescan)
    try:
        while limit is None or result.scanned < limit:
            count = page_size if limit is None else min(page_size, limit - result.scanned)
            page_started = time.monotonic()
            emit("page_requested", cursor=cursor, requested=count)
            page = await read_with_retry(
                lambda count=count, cursor=cursor: client.get_messages(
                    entity, limit=count, min_id=cursor, reverse=True, wait_time=0
                ),
                policy=retry_policy,
                progress=progress,
                operation_name="history",
            )
            if not page:
                result.status = "idle"
                break
            ids = [message.id for message in page]
            if ids != sorted(set(ids)) or ids[0] <= cursor or len(ids) > count:
                raise RecoveryError(
                    "Paginação inesperada; checkpoint anterior preservado.",
                    code="pagination_invalid",
                )
            records = []
            observed = []
            for message in page:
                try:
                    record = extract_video(message, channel_id)
                except Exception:
                    emit(
                        "message_parse_failed", message_id=message.id, error_code="metadata_invalid"
                    )
                    raise
                if record is not None:
                    observed.append(record)
                    if filters.matches(record.tags):
                        records.append(record)
            inserted, updated = database.save_page(records, channel_id, scope, ids[-1])
            cursor = ids[-1]
            result.last_message_id = cursor
            result.scanned += len(page)
            result.videos_seen += len(observed)
            result.matched += len(records)
            result.inserted += inserted
            result.updated += updated
            result.without_tags += sum(not record.tags for record in records)
            # Update diagnostics only after the page and cursor have committed successfully.
            discovery.add(observed, len(page), len(records))
            page_stats = Discovery()
            page_stats.add(observed, len(page), len(records))
            emit(
                "page_committed",
                first_message_id=ids[0],
                cursor=cursor,
                scanned=len(page),
                videos=len(observed),
                selected=len(records),
                inserted=inserted,
                updated=updated,
                duration_ms=round((time.monotonic() - page_started) * 1000, 2),
                counts=dict(page_stats.counts),
                mime_counts=dict(page_stats.mime_counts),
            )
            progress(
                f"Lidas={result.scanned}; vídeos={result.videos_seen}; "
                f"selecionados={result.matched}; novos={result.inserted}; cursor={cursor}."
            )
            if limit is None or result.scanned < limit:
                await asyncio.sleep(page_delay)
        else:
            result.status = "limited"
        database.set_scan(channel_id, scope, cursor, result.status)
    except (KeyboardInterrupt, asyncio.CancelledError):
        result.status = "interrupted"
        database.set_scan(channel_id, scope, cursor, "interrupted")
        raise
    except Exception:
        result.status = "failed"
        database.set_scan(channel_id, scope, cursor, "failed")
        raise
    finally:
        emit(
            "inventory_finished",
            status=result.status,
            start_cursor=start_cursor,
            cursor=cursor,
            scanned=result.scanned,
            messages_per_second=round(result.scanned / max(time.monotonic() - started, 0.001), 2),
            history_exhausted=result.status == "idle",
            scanned_from_start=start_cursor == 0,
            discovery=discovery.snapshot(),
        )
    return result
