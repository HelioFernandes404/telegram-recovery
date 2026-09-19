"""Private per-run JSONL events with allowlisted fields; no raw logging or tracebacks."""

import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import telethon
from telethon import errors

from . import __version__
from .security import RecoveryError, private_directory

ACTIVE_RUN = ContextVar("telegram_recovery_run", default=None)
ERROR_CODES = {
    "recovery_error",
    "already_running",
    "session_missing",
    "credentials_invalid",
    "session_unauthorized",
    "bot_session",
    "channel_inaccessible",
    "flood_wait_limit",
    "flood_wait",
    "read_retries_exhausted",
    "pagination_invalid",
    "message_mismatch",
    "manifest_version",
    "metadata_invalid",
    "unauthorized",
    "forbidden",
    "bad_request",
    "telegram_server",
    "telegram_rpc",
    "timeout",
    "network",
    "local_io",
    "sqlite",
    "unexpected",
    "interrupted",
    "auth_terminal_required",
    "auth_input_closed",
    "auth_attempts_exhausted",
    "auth_config_changed",
    "auth_flood_wait",
    "auth_network",
    "auth_phone_invalid",
    "auth_account_missing",
    "auth_flow_unsupported",
    "auth_code_expired",
    "auth_dc_migration",
    "value_error",
    "runtime_error",
    "type_error",
    "path_invalid",
    "file_conflict",
    "checksum_mismatch",
    "media_changed",
    "message_missing",
    "size_mismatch",
    "file_reference_expired",
    "download_retries_exhausted",
    "download_failed",
    "audit_failed",
    "batch_plan_changed",
}
OPERATIONS = {
    "connect",
    "authorization",
    "user",
    "channel",
    "dialogs",
    "history",
    "message",
    "read",
    "auth_send_code",
    "auth_sign_in",
    "auth_password",
}
METRICS = {
    "pages",
    "scanned",
    "videos",
    "non_video",
    "selected",
    "filtered_out",
    "without_tags",
    "multiple_tags",
    "missing_filename",
    "missing_mime",
    "unknown_size",
    "missing_duration",
    "structured_captions",
    "expected_bytes",
    "duration_seconds",
}
MIMES = {
    "video/mp4",
    "video/webm",
    "video/x-matroska",
    "video/quicktime",
    "missing",
    "other_video",
    "other",
}


def number(value):
    return type(value) in (int, float) and not isinstance(value, bool)


def boolean(value):
    return type(value) is bool


def optional_number(value):
    return value is None or number(value)


def number_list(value):
    return isinstance(value, (list, tuple)) and all(type(item) is int for item in value)


def version(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value)


def counters(keys):
    return lambda value: (
        isinstance(value, dict)
        and all(key in keys and number(count) for key, count in value.items())
    )


def clean_fields(fields, schema):
    # Reject unknown fields and invalid values by omission. Never stringify rejected objects.
    return {key: value for key, value in fields.items() if key in schema and schema[key](value)}


DISCOVERY_SCHEMA = {
    "counts": counters(METRICS),
    "mime_counts": counters(MIMES),
    "distinct_tags": number,
    "min_tag": optional_number,
    "max_tag": optional_number,
    "repeated_tags": number,
    "repeated_tag_sample": number_list,
    "unobserved_tag_numbers": number,
    "gap_range_sample": lambda value: (
        isinstance(value, list) and all(number_list(pair) and len(pair) == 2 for pair in value)
    ),
}
OPERATION_SCHEMA = {
    "operation": lambda value: value in OPERATIONS if isinstance(value, str) else False,
    "attempt": number,
    "max_attempts": number,
    "duration_ms": number,
    "delay_seconds": number,
    "error_code": lambda value: value in ERROR_CODES if isinstance(value, str) else False,
}
DOWNLOAD_SCHEMA = {
    "channel_id": number,
    "message_id": number,
    "bytes_downloaded": number,
    "expected_size": number,
    "attempt": number,
    "max_attempts": number,
    "delay_seconds": number,
    "error_code": OPERATION_SCHEMA["error_code"],
}
SCHEMAS = {
    "download_started": {
        "channel_id": number,
        "selected": number,
        "concurrency": number,
        "course": optional_number,
        "module": optional_number,
    },
    "download_attempt": DOWNLOAD_SCHEMA,
    "download_progress": DOWNLOAD_SCHEMA,
    "download_retry": DOWNLOAD_SCHEMA,
    "download_failed": DOWNLOAD_SCHEMA,
    "download_completed": DOWNLOAD_SCHEMA,
    "download_skipped": DOWNLOAD_SCHEMA,
    "module_checked": {
        "channel_id": number,
        "course": optional_number,
        "module": optional_number,
        "known_videos": number,
        "selected_videos": number,
        "verified_videos": number,
        "complete": boolean,
    },
    "download_finished": {
        key: number for key in ("selected", "downloaded", "skipped", "failed", "modules_skipped")
    },
    "auth_started": {"configure": boolean, "python_version": version, "telethon_version": version},
    "auth_prompt": {
        "field": lambda value: value in ("api_id", "api_hash", "phone", "code", "password"),
        "attempt": number,
    },
    "auth_step": {
        "stage": lambda value: (
            value
            in (
                "configuration_saved",
                "connected",
                "code_requested",
                "password_required",
                "authorized",
                "already_authorized",
            )
        )
    },
    "auth_request": OPERATION_SCHEMA,
    "auth_dc_redirect": OPERATION_SCHEMA,
    "auth_retry": {"field": lambda value: value in ("code", "password"), "attempt": number},
    "run_started": {
        "channel_id": number,
        "message_id": optional_number,
        "limit": optional_number,
        "page_size": number,
        "rescan": boolean,
        "tag_numbers": number_list,
        "from_tag": optional_number,
        "to_tag": optional_number,
        "python_version": version,
        "telethon_version": version,
        "app_version": version,
        "ffprobe_available": boolean,
    },
    "preflight": {"session_exists": boolean, "manifest_exists": boolean},
    "read_started": OPERATION_SCHEMA,
    "read_finished": OPERATION_SCHEMA,
    "read_retry": OPERATION_SCHEMA,
    "read_failed": OPERATION_SCHEMA,
    "channel_cache_miss": OPERATION_SCHEMA,
    "channel_resolved": {"source": lambda value: value in ("cache", "dialogs")},
    "inventory_started": {"cursor": number, "rescan": boolean},
    "page_requested": {"cursor": number, "requested": number},
    "message_requested": {"message_id": number},
    "message_inventory_finished": {
        "message_id": number,
        "outcome": lambda value: value in ("video", "missing", "non_video"),
        "inserted": number,
        "updated": number,
    },
    "message_parse_failed": {"message_id": number, "error_code": OPERATION_SCHEMA["error_code"]},
    "page_committed": {
        "first_message_id": number,
        "cursor": number,
        "scanned": number,
        "videos": number,
        "selected": number,
        "inserted": number,
        "updated": number,
        "duration_ms": number,
        "counts": counters(METRICS),
        "mime_counts": counters(MIMES),
    },
    "inventory_finished": {
        "status": lambda value: value in ("idle", "limited", "failed", "interrupted"),
        "start_cursor": number,
        "cursor": number,
        "scanned": number,
        "messages_per_second": number,
        "history_exhausted": boolean,
        "scanned_from_start": boolean,
        "discovery": lambda value: (
            isinstance(value, dict) and value == clean_fields(value, DISCOVERY_SCHEMA)
        ),
    },
    "run_finished": {
        "status": lambda value: value in ("success", "failed", "interrupted"),
        "exit_code": number,
        "error_code": OPERATION_SCHEMA["error_code"],
        "errno": number,
        "retry_count": number,
        "retry_wait_seconds": number,
        "read_calls": number,
        "last_committed_cursor": optional_number,
    },
}


def error_details(exc: BaseException) -> dict:
    if isinstance(exc, RecoveryError):
        return {"error_code": exc.code if exc.code in ERROR_CODES else "recovery_error"}
    categories = (
        ((KeyboardInterrupt, asyncio.CancelledError), "interrupted"),
        (errors.ChannelPrivateError, "channel_inaccessible"),
        (errors.UnauthorizedError, "unauthorized"),
        (errors.ForbiddenError, "forbidden"),
        (errors.BadRequestError, "bad_request"),
        (errors.ServerError, "telegram_server"),
        (errors.RPCError, "telegram_rpc"),
        (TimeoutError, "timeout"),
        (ConnectionError, "network"),
        (sqlite3.Error, "sqlite"),
        (OSError, "local_io"),
        (ValueError, "value_error"),
        (RuntimeError, "runtime_error"),
        (TypeError, "type_error"),
    )
    for kind, code in categories:
        if isinstance(exc, kind):
            details = {"error_code": code}
            if isinstance(exc, OSError) and type(exc.errno) is int:
                details["errno"] = exc.errno
            return details
    return {"error_code": "unexpected"}


def emit(event: str, **fields):
    run = ACTIVE_RUN.get()
    if run is not None:
        run.emit(event, **fields)


class RunLog:
    def __init__(self, root: Path, *, verbose: bool = False, command: str = "inventory"):
        if command not in ("inventory", "auth", "download"):
            raise ValueError("Unsupported log command")
        self.root = root
        self.verbose = verbose
        self.run_id = uuid.uuid4().hex
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = root / "logs" / f"{command}-{stamp}-{self.run_id}.jsonl"
        self.started = time.monotonic()
        self.sequence = 0
        self.retry_count = 0
        self.retry_wait_seconds = 0
        self.read_calls = 0
        self.last_cursor = None

    def __enter__(self):
        private_directory(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        self.token = ACTIVE_RUN.set(self)
        self.progress(f"Registro local: logs/{self.path.name}")
        return self

    def emit(self, event: str, **fields):
        safe = clean_fields(fields, SCHEMAS[event])
        if event in ("read_retry", "download_retry"):
            self.retry_count += 1
            self.retry_wait_seconds += safe.get("delay_seconds", 0)
        if event == "read_started":
            self.read_calls += 1
        if event in ("inventory_started", "page_committed"):
            self.last_cursor = safe.get("cursor")
        self.sequence += 1
        level = (
            "ERROR"
            if event in ("read_failed", "message_parse_failed", "download_failed")
            or safe.get("status") == "failed"
            else (
                "WARNING"
                if event in ("read_retry", "download_retry", "auth_retry")
                or safe.get("status") == "interrupted"
                else "INFO"
            )
        )
        row = {
            "schema_version": 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event": event,
            "level": level,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            **safe,
        }
        self.stream.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n")
        self.stream.flush()
        if self.verbose:
            self.progress(f"{event} {json.dumps(safe, ensure_ascii=True)}")
        if event == "inventory_finished" and "discovery" in safe:
            stats = safe["discovery"]
            counts = stats.get("counts", {})
            self.progress(
                f"Discovery desta execução: sem_tag={counts.get('without_tags', 0)}; "
                f"tamanho_desconhecido={counts.get('unknown_size', 0)}; "
                f"tags_repetidas={stats.get('repeated_tags', 0)}; "
                f"números_não_observados={stats.get('unobserved_tag_numbers', 0)}. "
                "Estatísticas das páginas confirmadas; lacunas não comprovam vídeos faltantes."
            )

    def start(self, args, filters):
        self.emit(
            "run_started",
            channel_id=args.channel,
            message_id=getattr(args, "message_id", None),
            limit=args.limit,
            page_size=args.page_size,
            rescan=args.rescan,
            tag_numbers=[int(tag[1:]) for tag in filters.tags],
            from_tag=int(filters.from_tag[1:]) if filters.from_tag else None,
            to_tag=int(filters.to_tag[1:]) if filters.to_tag else None,
            python_version=".".join(map(str, sys.version_info[:3])),
            telethon_version=telethon.__version__,
            app_version=__version__,
            ffprobe_available=shutil.which("ffprobe") is not None,
        )
        self.emit(
            "preflight",
            session_exists=(self.root / "sessions/telegram-recovery.session").is_file(),
            manifest_exists=(self.root / "data/manifest.sqlite3").is_file(),
        )

    def start_auth(self, *, configure: bool):
        self.emit(
            "auth_started",
            configure=configure,
            python_version=".".join(map(str, sys.version_info[:3])),
            telethon_version=telethon.__version__,
        )
        self.emit(
            "preflight",
            session_exists=(self.root / "sessions/telegram-recovery.session").is_file(),
            manifest_exists=(self.root / "data/manifest.sqlite3").is_file(),
        )

    def progress(self, message):
        # Only fixed application messages go here. This text is deliberately not persisted.
        stamp = datetime.now(UTC).strftime("%H:%M:%SZ")
        print(f"[{stamp} {self.run_id[:8]}] {message}", file=sys.stderr, flush=True)

    def __exit__(self, exc_type, exc, traceback):
        interrupted = isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
        try:
            self.emit(
                "run_finished",
                status="success" if exc is None else ("interrupted" if interrupted else "failed"),
                exit_code=0 if exc is None else 130 if interrupted else 1,
                **(error_details(exc) if exc is not None else {}),
                retry_count=self.retry_count,
                retry_wait_seconds=self.retry_wait_seconds,
                read_calls=self.read_calls,
                last_committed_cursor=self.last_cursor,
            )
            os.fsync(self.stream.fileno())
            if exc is not None:
                self.progress(
                    f"Diagnóstico: {error_details(exc)['error_code']}; consulte o log local."
                )
        except OSError:
            self.progress("Falha ao finalizar o log local; confira espaço e permissões em logs/.")
            if exc is None:
                raise
        finally:
            ACTIVE_RUN.reset(self.token)
            self.stream.close()
