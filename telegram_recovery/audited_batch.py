"""Frozen, resumable download batches with a private local audit trail."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

from .database import Database, utc_now
from .download_files import destination, file_size, sha256_file, sync_directory
from .download_plan import build_plan
from .downloader import download_one
from .run_logging import RunLog, error_details
from .security import RecoveryError, exclusive_lock, private_directory, private_file
from .telegram_client import existing_user_session, resolve_channel


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()


def atomic_json(path, value):
    private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name)


def job_directory(root, job):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", job):
        raise RecoveryError("Nome do lote inválido.", code="path_invalid")
    return root / "data" / "batches" / job


def identity(item):
    return {
        "message_id": item.video["message_id"],
        "document_id": item.video["document_id"],
        "expected_size": item.video["expected_size"],
        "relative_path": item.relative_path,
        "course": item.course,
        "module": item.module,
    }


def prepare(root, job, channel, excluded):
    folder = job_directory(root, job)
    private_directory(folder)
    with exclusive_lock(root / "data/.inventory.lock"):
        with Database(root / "data/manifest.sqlite3", readonly=True) as db:
            items = [i for i in build_plan(db, channel) if i.course not in excluded]
        if not items or any(
            type(i.video["expected_size"]) is not int or i.video["expected_size"] <= 0
            for i in items
        ):
            raise RecoveryError("Seleção vazia ou tamanho inválido.", code="metadata_invalid")
        plan = {
            "schema_version": 1,
            "created_at": utc_now(),
            "channel_id": channel,
            "excluded_courses": sorted(set(excluded)),
            "items": [identity(i) for i in items],
        }
        # The scope is immutable: preparing again must never replace an existing plan.
        fd = os.open(folder / "plan.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(plan) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(folder)
    return plan


def load_selection(db, plan):
    if plan.get("schema_version") != 1 or type(plan.get("channel_id")) is not int:
        raise RecoveryError("Plano inválido.", code="batch_plan_changed")
    current = {i.video["message_id"]: i for i in build_plan(db, plan["channel_id"])}
    selected, seen, paths = [], set(), set()
    for saved in plan.get("items", []):
        mid = saved.get("message_id")
        item = current.get(mid)
        if (
            item is None
            or mid in seen
            or identity(item) != saved
            or item.relative_path in paths
            or item.course in plan.get("excluded_courses", [])
            or type(item.video["expected_size"]) is not int
            or item.video["expected_size"] <= 0
        ):
            raise RecoveryError(
                "Plano diverge do manifesto; revise o lote.", code="batch_plan_changed"
            )
        selected.append(item)
        seen.add(mid)
        paths.add(item.relative_path)
    if not selected:
        raise RecoveryError("Plano vazio.", code="batch_plan_changed")
    return selected


def signature(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def audit_file(root, item, saved, probe):
    """Local size/hash/container check. This does not decode every video frame."""
    result = {
        "ok": False,
        "error_code": None,
        "sha256": None,
        "ffprobe": "pending" if probe else "unavailable",
        "signature": None,
    }
    if (
        not saved
        or saved["document_id"] != item.video["document_id"]
        or saved["expected_size"] != item.video["expected_size"]
        or saved["relative_path"] != item.relative_path
    ):
        result["error_code"] = "media_changed"
        return result
    try:
        path = destination(root, item.relative_path, item.video["channel_id"], create=False)
        size = file_size(path)
        if size is None or size != item.video["expected_size"]:
            result["error_code"] = "size_mismatch"
            return result
        before = signature(path)
        result["sha256"] = sha256_file(path)
        if not saved["sha256"] or result["sha256"] != saved["sha256"]:
            result["error_code"] = "checksum_mismatch"
            return result
        if path.with_name(path.name + ".part").exists():
            result["error_code"] = "file_conflict"
            return result
        if probe:
            process = subprocess.run(
                [
                    probe,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-show_entries",
                    "stream=codec_type,codec_name:format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
            data = json.loads(process.stdout) if process.returncode == 0 else {}
            if (
                process.returncode
                or process.stderr.strip()
                or not any(s.get("codec_type") == "video" for s in data.get("streams", []))
            ):
                result.update(error_code="audit_failed", ffprobe="failed")
                return result
            result["ffprobe"] = "passed"
        if signature(path) != before:
            result["error_code"] = "file_conflict"
            return result
        result.update(ok=True, signature=before)
    except (OSError, RecoveryError, subprocess.SubprocessError, ValueError) as exc:
        result["error_code"] = error_details(exc)["error_code"]
    return result


class AuditStore:
    def __init__(self, path, digest):
        private_file(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS batch (plan_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audits (
                    message_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL, ok INTEGER NOT NULL,
                    sha256 TEXT, duplicate_of INTEGER, details TEXT NOT NULL
                );
            """)
            row = self.db.execute("SELECT plan_sha256 FROM batch").fetchone()
            if row and row[0] != digest:
                raise RecoveryError(
                    "Plano alterado desde a primeira auditoria.", code="batch_plan_changed"
                )
            if not row:
                with self.db:
                    self.db.execute("INSERT INTO batch VALUES (?)", (digest,))
        except BaseException:
            self.db.close()
            raise

    def save(self, mid, run_id, result):
        duplicate = (
            self.db.execute(
                "SELECT message_id FROM audits WHERE sha256=? AND ok=1 AND message_id!=? "
                "AND run_id=? ORDER BY message_id LIMIT 1",
                (result["sha256"], mid, run_id),
            ).fetchone()
            if result["ok"]
            else None
        )
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO audits VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    mid,
                    run_id,
                    utc_now(),
                    int(result["ok"]),
                    result["sha256"],
                    duplicate[0] if duplicate else None,
                    canonical(result).decode(),
                ),
            )

    def close(self):
        self.db.close()


def failure_code(exc):
    if isinstance(exc, BaseExceptionGroup):
        return failure_code(exc.exceptions[0])
    return error_details(exc)["error_code"]


async def execute(root, folder, plan, items, db, audits, log, concurrency):
    channel = plan["channel_id"]
    probe = shutil.which("ffprobe")
    verified, active = {}, set()
    state = {
        "state": "checking",
        "channel_id": channel,
        "run_id": log.run_id,
        "started_at": utc_now(),
        "last_activity_at": utc_now(),
        "selected": len(items),
        "courses": len({i.course for i in items}),
        "expected_bytes": sum(i.video["expected_size"] for i in items),
        "concurrency": concurrency,
        "ffprobe_available": bool(probe),
        "log": str(log.path.relative_to(root)),
        "error_code": None,
    }
    mids = {i.video["message_id"] for i in items}

    def progress(message):
        state["last_activity_at"] = utc_now()
        log.progress(message)

    def snapshot():
        rows = [
            dict(r)
            for r in db.connection.execute(
                "SELECT message_id,status,bytes_downloaded FROM downloads WHERE channel_id=?",
                (channel,),
            )
            if r["message_id"] in mids
        ]
        state.update(
            updated_at=utc_now(),
            verified=len(verified),
            downloaded=sum(r["status"] == "downloaded" for r in rows),
            bytes_downloaded=sum(r["bytes_downloaded"] for r in rows),
            pending=len(items) - len(verified),
            active_message_ids=sorted(active),
            duplicates=audits.db.execute(
                "SELECT COUNT(*) FROM audits WHERE run_id=? AND duplicate_of IS NOT NULL",
                (log.run_id,),
            ).fetchone()[0],
        )
        atomic_json(folder / "status.json", state)

    async def audit(item):
        mid = item.video["message_id"]
        result = await asyncio.to_thread(audit_file, root, item, db.download(channel, mid), probe)
        audits.save(mid, log.run_id, result)
        if not result["ok"]:
            state["failed_message_id"] = mid
            raise RecoveryError("Auditoria falhou; arquivo preservado.", code=result["error_code"])
        verified[mid] = result["signature"]
        progress(f"Mensagem {mid}: auditoria concluída ({len(verified)}/{len(items)}).")
        snapshot()

    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            snapshot()

    async def work():
        pending = []
        # Audit existing finals first; a conflict cannot be hidden behind a skipped download.
        for item in items:
            path = destination(root, item.relative_path, channel, create=False)
            if file_size(path) is not None:
                await audit(item)
            else:
                pending.append(item)
        if pending:
            remaining = sum(i.video["expected_size"] for i in pending)
            if shutil.disk_usage(root).free < remaining + 1024**3:
                raise RecoveryError("Espaço livre insuficiente para o lote.", code="local_io")
            state["state"] = "downloading"
            snapshot()
            async with existing_user_session(root, progress=progress) as client:
                entity = await resolve_channel(client, channel, progress=progress)
                queue = asyncio.Queue()
                for item in pending:
                    queue.put_nowait(item)

                async def worker():
                    while not queue.empty():
                        item = queue.get_nowait()
                        mid = item.video["message_id"]
                        active.add(mid)
                        try:
                            await download_one(client, entity, db, root, item, progress=progress)
                            await audit(item)
                        except Exception:
                            state.setdefault("failed_message_id", mid)
                            raise
                        finally:
                            active.discard(mid)
                            queue.task_done()

                async with asyncio.TaskGroup() as group:
                    for _ in range(min(concurrency, len(pending))):
                        group.create_task(worker())
        # Detect modification of an already audited file while the rest was downloading.
        for item in items:
            path = destination(root, item.relative_path, channel, create=False)
            if (
                file_size(path) != item.video["expected_size"]
                or signature(path) != verified[item.video["message_id"]]
                or path.with_name(path.name + ".part").exists()
            ):
                raise RecoveryError("Arquivo mudou durante o lote.", code="file_conflict")

    snapshot()
    try:
        async with asyncio.TaskGroup() as group:
            ticker = group.create_task(heartbeat())
            try:
                await work()
            finally:
                ticker.cancel()
    except BaseException as exc:
        code = failure_code(exc)
        state.update(
            state="interrupted" if code == "interrupted" else "needs_attention", error_code=code
        )
        snapshot()
        raise
    else:
        state.update(state="completed", completed_at=utc_now())
        snapshot()
    return state


async def run(root, job, concurrency):
    folder = job_directory(root, job)
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, owner.cancel)
    try:
        with exclusive_lock(root / "data/.inventory.lock"):
            with Database(root / "data/manifest.sqlite3") as db:
                plan = json.loads((folder / "plan.json").read_bytes())
                items = load_selection(db, plan)
                digest = hashlib.sha256(canonical(plan)).hexdigest()
                audits = AuditStore(folder / "audit.sqlite3", digest)
                try:
                    with RunLog(root, command="download") as log:
                        log.emit(
                            "download_started",
                            channel_id=plan["channel_id"],
                            selected=len(items),
                            concurrency=concurrency,
                        )
                        try:
                            return await execute(
                                root, folder, plan, items, db, audits, log, concurrency
                            )
                        except BaseException as exc:
                            # Normalize ExceptionGroup so only a safe code reaches the log.
                            raise RecoveryError(
                                "Lote interrompido; consulte status.json.", code=failure_code(exc)
                            ) from None
                finally:
                    audits.close()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("prepare", help="Congela uma seleção local, sem Telegram")
    create.add_argument("--job", required=True)
    create.add_argument("--channel", type=int, required=True)
    create.add_argument("--exclude-course", type=int, action="append", default=[])
    start = commands.add_parser("run", help="Baixa/retoma e audita o plano existente")
    start.add_argument("--job", required=True)
    start.add_argument("--concurrency", type=int, choices=range(1, 9), default=1)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "prepare":
            plan = prepare(args.root.resolve(), args.job, args.channel, args.exclude_course)
            print(
                json.dumps(
                    {
                        "selected": len(plan["items"]),
                        "expected_bytes": sum(i["expected_size"] for i in plan["items"]),
                    }
                )
            )
        else:
            asyncio.run(run(args.root.resolve(), args.job, args.concurrency))
        return 0
    except BaseException as exc:
        # Never print provider errors, tracebacks, account data or environment values.
        code = failure_code(exc)
        print(f"Lote encerrado: {code}.", flush=True)
        return 130 if code == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
