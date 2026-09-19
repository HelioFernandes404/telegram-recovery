"""MTProto reads using an existing user session; never initiates authentication."""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from telethon import TelegramClient, errors, types, utils

from .run_logging import emit, error_details
from .security import RecoveryError, exclusive_lock, private_directory, private_file


@dataclass(frozen=True)
class Credentials:
    api_id: int = field(repr=False)
    api_hash: str = field(repr=False)


def load_credentials(root: Path) -> Credentials:
    # No search in parent directories and no interpolation of unrelated environment values.
    env_file = root / ".env"
    private_file(env_file)
    values = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}
    api_id = os.environ.get("TELEGRAM_API_ID", values.get("TELEGRAM_API_ID") or "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", values.get("TELEGRAM_API_HASH") or "")
    if not re.fullmatch(r"[0-9]{1,10}", api_id) or not (0 < int(api_id) < 2**31):
        raise RecoveryError(
            "Configure TELEGRAM_API_ID no ambiente ou no .env local.", code="credentials_invalid"
        )
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise RecoveryError(
            "Configure TELEGRAM_API_HASH no ambiente ou no .env local.", code="credentials_invalid"
        )
    return Credentials(int(api_id), api_hash)


def channel_number(value: str) -> int:
    try:
        number = int(value)
        raw_id, peer_type = utils.resolve_id(number)
    except ValueError:
        raise ValueError("Informe o ID numérico do canal, como -1001234567890.") from None
    if peer_type is not types.PeerChannel or not (0 < raw_id < 2**63):
        raise ValueError("Informe um ID de canal no formato -100…, como -1001234567890.")
    return number


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    timeout: float = 45
    max_flood_wait: int = 300


async def read_with_retry(
    operation, *, policy: RetryPolicy | None = None, progress=lambda _: None, operation_name="read"
):
    """Retry only transient failures; never format the provider's exception or request."""
    policy = policy or RetryPolicy()
    for attempt in range(1, policy.attempts + 1):
        started = time.monotonic()
        context = {"operation": operation_name, "attempt": attempt, "max_attempts": policy.attempts}
        emit("read_started", **context)
        try:
            result = await asyncio.wait_for(operation(), timeout=policy.timeout)
        except errors.FloodWaitError as exc:
            if exc.seconds > policy.max_flood_wait or attempt == policy.attempts:
                emit(
                    "read_failed",
                    **context,
                    error_code="flood_wait_limit",
                    delay_seconds=exc.seconds,
                )
                raise RecoveryError(
                    f"Telegram pediu espera de {exc.seconds}s. Retome o inventário depois.",
                    code="flood_wait_limit",
                ) from None
            delay = exc.seconds
            reason = "flood_wait"
            progress(f"FloodWait: aguardando {delay}s; tentativa {attempt}/{policy.attempts}.")
        except (OSError, TimeoutError, errors.ServerError) as exc:
            reason = (
                "timeout"
                if isinstance(exc, TimeoutError)
                else ("telegram_server" if isinstance(exc, errors.ServerError) else "network")
            )
            if attempt == policy.attempts:
                emit("read_failed", **context, error_code=reason)
                raise RecoveryError(
                    "Falha de rede/timeout após novas tentativas; retome o inventário depois.",
                    code="read_retries_exhausted",
                ) from None
            delay = min(2 ** (attempt - 1), 30)
            progress(f"Falha temporária; tentativa {attempt}/{policy.attempts}, espera {delay}s.")
        except Exception as exc:
            if isinstance(exc, ValueError) and operation_name == "channel":
                emit("channel_cache_miss", **context)
            else:
                emit("read_failed", **context, **error_details(exc))
            raise
        else:
            emit(
                "read_finished",
                **context,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            return result
        emit("read_retry", **context, error_code=reason, delay_seconds=delay)
        await asyncio.sleep(delay)


@asynccontextmanager
async def existing_user_session(root: Path, *, progress=lambda _: None):
    session_dir = root / "sessions"
    session_path = session_dir / "telegram-recovery.session"
    private_directory(session_dir)
    private_file(session_path)
    if not session_path.is_file():
        raise RecoveryError(
            "Sessão local ausente. Nenhum login foi iniciado; "
            "o inventário exige uma sessão de usuário já autenticada. "
            "Execute auth no terminal local para autenticar explicitamente.",
            code="session_missing",
        )
    credentials = load_credentials(root)
    with exclusive_lock(session_dir / ".inventory.lock"):
        for file in session_dir.glob("*.session*"):
            private_file(file)
        client = create_client(session_path, credentials)
        try:
            await read_with_retry(client.connect, progress=progress, operation_name="connect")
            authorized = await read_with_retry(
                client.is_user_authorized, progress=progress, operation_name="authorization"
            )
            if not authorized:
                raise RecoveryError(
                    "Sessão não autenticada ou expirada. Nenhum login foi iniciado.",
                    code="session_unauthorized",
                )
            user = await read_with_retry(client.get_me, progress=progress, operation_name="user")
            if user is None or user.bot:
                raise RecoveryError(
                    "É necessária uma sessão de conta de usuário, não de bot.", code="bot_session"
                )
            yield client
        finally:
            try:
                await client.disconnect()
            finally:
                for file in session_dir.glob("*.session*"):
                    private_file(file)


def create_client(session_path: Path, credentials: Credentials):
    # Suppress Telethon logs: a provider error/request could contain account information.
    logger = logging.getLogger("telegram_recovery.private_telethon")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return TelegramClient(
        str(session_path),
        credentials.api_id,
        credentials.api_hash,
        receive_updates=False,
        flood_sleep_threshold=0,
        request_retries=0,
        raise_last_call_error=True,
        connection_retries=2,
        timeout=15,
        base_logger=logger,
    )


async def resolve_channel(client, channel_id: int, *, progress=lambda _: None):
    try:
        entity = await read_with_retry(
            lambda: client.get_input_entity(channel_id), progress=progress, operation_name="channel"
        )
    except ValueError:
        # A numeric channel ID alone lacks access_hash. Reading dialogs populates the cache.
        # No joining, invitations, or message mutations are performed.
        async def lookup_dialog():
            async for dialog in client.iter_dialogs():
                if dialog.id == channel_id:
                    return dialog.input_entity
            raise RecoveryError(
                "Canal não encontrado entre os canais acessíveis à conta.",
                code="channel_inaccessible",
            )

        entity = await read_with_retry(lookup_dialog, progress=progress, operation_name="dialogs")
        emit("channel_resolved", source="dialogs")
    else:
        emit("channel_resolved", source="cache")
    return entity
