"""Bounded MTProto downloads into owned partials, grouped by course/module."""

import asyncio
import hashlib
import os
import stat
import time
from dataclasses import dataclass

from telethon import errors

from .database import utc_now
from .download_files import destination, file_size, finalize, sha256_file
from .inventory import extract_video
from .run_logging import emit, error_details
from .security import RecoveryError
from .telegram_client import read_with_retry


@dataclass(frozen=True)
class DownloadPolicy:
    attempts: int = 4
    timeout: float = 45
    max_flood_wait: int = 300
    progress_interval: float = 2


class TransferNetworkError(RecoveryError):
    pass


def seed_digest(stream, saved):
    """Validate the durable prefix, then incorporate bytes after that checkpoint."""
    digest = hashlib.sha256()
    remaining = saved["bytes_downloaded"]
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        if not chunk:
            raise RecoveryError("Parcial menor que o checkpoint; preservado.", code="file_conflict")
        digest.update(chunk)
        remaining -= len(chunk)
    if saved["partial_sha256"] and digest.hexdigest() != saved["partial_sha256"]:
        raise RecoveryError("SHA-256 do parcial diverge; preservado.", code="checksum_mismatch")
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest


async def download_one(
    client, entity, database, root, item, *, policy=None, progress=lambda _: None
):
    policy = policy or DownloadPolicy()
    video = item.video
    channel_id, message_id = video["channel_id"], video["message_id"]
    expected = video["expected_size"]
    context = {"channel_id": channel_id, "message_id": message_id}

    def update(**fields):
        database.update_download(channel_id, message_id, **fields)

    try:
        if type(expected) is not int or expected <= 0:
            raise RecoveryError(
                "Tamanho esperado ausente/inválido; revise o inventário.", code="metadata_invalid"
            )
        final = destination(root, item.relative_path, channel_id)
        part = final.with_name(final.name + ".part")
        saved = database.download(channel_id, message_id)
        if saved is None:
            database.add_download(video, item.relative_path)
            saved = database.download(channel_id, message_id)
        if saved["document_id"] != video["document_id"] or saved["expected_size"] != expected:
            raise RecoveryError(
                "Mídia mudou; arquivos anteriores preservados.", code="media_changed"
            )
        if saved["relative_path"] != item.relative_path:
            raise RecoveryError("Destino mudou; arquivo preservado.", code="path_invalid")
        size = file_size(final)
        if size is not None:
            if (
                size != expected
                or not saved["sha256"]
                or await asyncio.to_thread(sha256_file, final) != saved["sha256"]
            ):
                raise RecoveryError(
                    "Destino existente não validado; preservado.", code="file_conflict"
                )
            update(
                status="downloaded",
                error_code=None,
                downloaded_at=saved["downloaded_at"] or utc_now(),
            )
            emit("download_skipped", **context, bytes_downloaded=size)
            progress(f"Mensagem {message_id}: arquivo existente conferido; ignorado.")
            return "skipped"

        for attempt in range(1, policy.attempts + 1):
            saved = database.download(channel_id, message_id)
            update(status="downloading", attempts=saved["attempts"] + 1, error_code=None)
            emit("download_attempt", **context, attempt=attempt, max_attempts=policy.attempts)
            try:
                # Refresh references and reject replacement media before resuming bytes.
                message = await read_with_retry(
                    lambda: client.get_messages(entity, ids=message_id),
                    progress=progress,
                    operation_name="message",
                )
                if message is None:
                    raise RecoveryError(
                        "Mensagem não disponível; parcial preservado.", code="message_missing"
                    )
                if message.id != message_id or message.chat_id != channel_id:
                    raise RecoveryError("Canal/mensagem divergente.", code="message_mismatch")
                fresh = extract_video(message, channel_id)
                if (
                    fresh is None
                    or fresh.document_id != video["document_id"]
                    or fresh.expected_size != expected
                ):
                    raise RecoveryError(
                        "Mídia mudou; atualize o inventário. Parcial preservado.",
                        code="media_changed",
                    )
                size = file_size(part)
                if size is None:
                    fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    try:
                        update(owned_part=1, bytes_downloaded=0, partial_sha256=None)
                    except BaseException:
                        os.close(fd)
                        raise
                else:
                    if not saved["owned_part"] or size > expected:
                        raise RecoveryError(
                            "Parcial existente não reconhecido; preservado.", code="file_conflict"
                        )
                    fd = os.open(part, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "r+b", buffering=0) as output:
                    if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                        raise RecoveryError(
                            "Parcial não regular; preservado.", code="file_conflict"
                        )
                    saved = database.download(channel_id, message_id)
                    digest = seed_digest(output, saved)
                    offset = output.tell()
                    if offset > expected:
                        raise RecoveryError(
                            "Parcial maior que o esperado; preservado.", code="file_conflict"
                        )
                    last_progress = 0.0

                    def checkpoint(digest=digest):
                        os.fsync(output.fileno())
                        update(bytes_downloaded=output.tell(), partial_sha256=digest.hexdigest())

                    if offset < expected:
                        stream = client.iter_download(
                            message.document,
                            offset=offset,
                            file_size=expected,
                            request_size=512 * 1024,
                        )
                        try:
                            while True:
                                try:
                                    # Keep cancellation on this task: wait_for can lose it
                                    # when a chunk completes concurrently on Python 3.11.
                                    async with asyncio.timeout(policy.timeout):
                                        chunk = await anext(stream)
                                except StopAsyncIteration:
                                    break
                                except OSError as exc:
                                    code = "timeout" if isinstance(exc, TimeoutError) else "network"
                                    raise TransferNetworkError(
                                        "Falha temporária de transferência.", code=code
                                    ) from None
                                if not chunk or output.tell() + len(chunk) > expected:
                                    raise RecoveryError(
                                        "Tamanho recebido inválido; parcial preservado.",
                                        code="size_mismatch",
                                    )
                                written = output.write(chunk)
                                if written != len(chunk):
                                    digest.update(chunk[: written or 0])
                                    raise OSError("Short local write")
                                digest.update(chunk)
                                now = time.monotonic()
                                if now - last_progress >= policy.progress_interval:
                                    checkpoint()
                                    emit(
                                        "download_progress",
                                        **context,
                                        bytes_downloaded=output.tell(),
                                        expected_size=expected,
                                    )
                                    progress(
                                        f"Mensagem {message_id}: {output.tell()}/{expected} bytes."
                                    )
                                    last_progress = now
                        finally:
                            try:
                                checkpoint()
                            finally:
                                await stream.close()
                    checkpoint()
                    if output.tell() != expected:
                        raise TransferNetworkError(
                            "Transferência incompleta; retomada necessária.", code="size_mismatch"
                        )
                checksum = await asyncio.to_thread(sha256_file, part)
                if saved["sha256"] and checksum != saved["sha256"]:
                    raise RecoveryError(
                        "SHA-256 difere do download anterior; parcial preservado.",
                        code="checksum_mismatch",
                    )
                update(status="ready", sha256=checksum, bytes_downloaded=expected)
                try:
                    finalize(part, final)
                except FileExistsError:
                    raise RecoveryError(
                        "Destino apareceu durante o download; preservado.", code="file_conflict"
                    ) from None
                update(status="downloaded", downloaded_at=utc_now(), error_code=None)
                emit("download_completed", **context, bytes_downloaded=expected)
                progress(f"Mensagem {message_id}: concluída; tamanho e SHA-256 registrados.")
                return "downloaded"
            except (errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError):
                code, delay = "file_reference_expired", 0
            except errors.FloodWaitError as exc:
                code, delay = "flood_wait", exc.seconds
                if delay > policy.max_flood_wait:
                    raise RecoveryError(
                        f"Telegram pediu espera de {delay}s; retome depois.",
                        code="flood_wait_limit",
                    ) from None
            except (TransferNetworkError, errors.ServerError) as exc:
                code = exc.code if isinstance(exc, TransferNetworkError) else "telegram_server"
                delay = min(2 ** (attempt - 1), 30)
            if attempt == policy.attempts:
                raise RecoveryError(
                    "Tentativas de download esgotadas; parcial preservado.",
                    code="download_retries_exhausted",
                )
            update(error_code=code)
            emit("download_retry", **context, attempt=attempt, error_code=code, delay_seconds=delay)
            progress(f"Mensagem {message_id}: {code}; nova tentativa em {delay}s.")
            await asyncio.sleep(delay)
    except BaseException as exc:
        code = error_details(exc)["error_code"]
        if database.download(channel_id, message_id):
            update(
                status="interrupted"
                if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
                else "failed",
                error_code=code,
            )
        emit("download_failed", **context, error_code=code)
        raise


async def download_items(
    client,
    entity,
    database,
    root,
    items,
    *,
    concurrency=1,
    policy=None,
    progress=lambda _: None,
    already_skipped=0,
    modules_skipped=0,
):
    if not 1 <= concurrency <= 8:
        raise ValueError("--concurrency deve estar entre 1 e 8.")
    queue = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)
    counts = {
        "selected": len(items) + already_skipped,
        "downloaded": 0,
        "skipped": already_skipped,
        "failed": 0,
        "modules_skipped": modules_skipped,
    }

    async def worker():
        while not queue.empty():
            item = queue.get_nowait()
            try:
                result = await download_one(
                    client, entity, database, root, item, policy=policy, progress=progress
                )
            except (RecoveryError, errors.RPCError, OSError, ValueError):
                counts["failed"] += 1
                progress(f"Mensagem {item.video['message_id']}: falhou; consulte o log local.")
            else:
                counts[result] += 1
            finally:
                queue.task_done()

    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(items))):
            group.create_task(worker())
    emit("download_finished", **counts)
    return counts
