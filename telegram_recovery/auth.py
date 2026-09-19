"""Explicit, terminal-only user login. Secrets are never printed or accepted as CLI flags."""

import asyncio
import getpass
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

from dotenv import dotenv_values
from telethon import errors

from . import telegram_client
from .run_logging import emit
from .security import RecoveryError, exclusive_lock, private_file

ATTEMPTS = 3
API_KEYS = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")


def require_terminal() -> None:
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        raise RecoveryError(
            "auth exige um terminal local interativo, sem redirecionamento de entrada/saída.",
            code="auth_terminal_required",
        )


def secret_input(prompt: str) -> str:
    require_terminal()
    try:
        with warnings.catch_warnings():
            # getpass must fail rather than fall back to echoing the input.
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt, stream=sys.stderr)
    except getpass.GetPassWarning:
        raise RecoveryError(
            "O terminal não permite entrada oculta. Use um terminal local compatível.",
            code="auth_terminal_required",
        ) from None
    except EOFError:
        raise RecoveryError(
            "Entrada encerrada; autenticação cancelada.", code="auth_input_closed"
        ) from None


def phone_input(prompt: str) -> str:
    """Phone is visible only in the user's terminal, by explicit user preference."""
    require_terminal()
    try:
        return input(prompt)
    except EOFError:
        raise RecoveryError(
            "Entrada encerrada; autenticação cancelada.", code="auth_input_closed"
        ) from None


def prompt_validated(kind, prompt, valid, *, normalize=lambda value: value.strip()):
    for attempt in range(1, ATTEMPTS + 1):
        emit("auth_prompt", field=kind, attempt=attempt)
        reader = phone_input if kind == "phone" else secret_input
        value = normalize(reader(prompt))
        if valid(value):
            return value
        print("Formato inválido. Tente novamente; o valor não será exibido.", file=sys.stderr)
    raise RecoveryError("Limite de tentativas de entrada atingido.", code="auth_attempts_exhausted")


def valid_api_id(value):
    return bool(re.fullmatch(r"[0-9]{1,10}", value)) and 0 < int(value) < 2**31


def valid_api_hash(value):
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", value))


def configure_credentials(root: Path):
    """Fill absent keys only, preserving existing content and environment precedence."""
    require_terminal()
    try:
        return telegram_client.load_credentials(root)
    except RecoveryError as exc:
        if exc.code != "credentials_invalid":
            raise
    # An explicit environment override cannot be repaired by writing .env.
    if any(key in os.environ for key in API_KEYS):
        raise RecoveryError(
            "Configuração do ambiente incompleta/inválida. Corrija as variáveis locais "
            "ou remova seus overrides antes de usar auth --configure.",
            code="credentials_invalid",
        )
    env_path = root / ".env"
    private_file(env_path)
    source = env_path.read_bytes().decode("utf-8") if env_path.exists() else ""
    values = dotenv_values(env_path, interpolate=False) if env_path.exists() else {}
    validators = dict(zip(API_KEYS, (valid_api_id, valid_api_hash), strict=True))
    for key in API_KEYS:
        if values.get(key) and not validators[key](values[key]):
            raise RecoveryError(
                "Há uma credencial preenchida, mas inválida, no .env. Corrija-a no editor local; "
                "o arquivo foi preservado.",
                code="credentials_invalid",
            )
    additions = {}
    print(
        "API ID/API hash: use os dados de API development tools em https://my.telegram.org. "
        "Digite somente neste terminal; Ctrl+C cancela.",
        file=sys.stderr,
    )
    for key, field in zip(API_KEYS, ("api_id", "api_hash"), strict=True):
        if not values.get(key):
            additions[key] = prompt_validated(field, f"{key} (entrada oculta): ", validators[key])
    # Append validated assignments, so comments and all unrelated keys remain byte-for-byte intact.
    # Empty placeholders remain above; dotenv uses the last occurrence of each key.
    updated = source + ("\n" if source and not source.endswith("\n") else "")
    updated += "# Credenciais configuradas localmente por telegram-recovery auth.\n"
    updated += "".join(f"{key}={value}\n" for key, value in additions.items())
    fd, temp_name = tempfile.mkstemp(prefix=".env.auth-", dir=root)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        # Avoid overwriting edits made in an editor while the credential prompts were open.
        private_file(env_path)
        current = env_path.read_bytes().decode("utf-8") if env_path.exists() else ""
        if current != source:
            raise RecoveryError(
                ".env foi alterado durante a configuração. Alterações preservadas; repita auth.",
                code="auth_config_changed",
            )
        os.replace(temp_path, env_path)
    finally:
        temp_path.unlink(missing_ok=True)
    emit("auth_step", stage="configuration_saved")
    print("Credenciais salvas no .env local protegido.", file=sys.stderr)
    return telegram_client.load_credentials(root)


async def auth_request(operation, operation_name):
    """Follow one definitive DC redirect, never retry ambiguous login responses."""
    for attempt in (1, 2):
        try:
            return await _auth_request_once(operation, operation_name)
        except (errors.PhoneMigrateError, errors.NetworkMigrateError, errors.UserMigrateError):
            # With request_retries=0 and raise_last_call_error=True, Telethon 1.x
            # has already switched the DC before exposing this rejection to us.
            # A 303 explicitly instructs a retry on the correct DC; it is not an
            # uncertain delivery or a request to resend an already accepted code.
            if attempt == 2:
                raise RecoveryError(
                    "Telegram pediu redirecionamentos repetidos. Sessão preservada; "
                    "execute auth novamente depois.",
                    code="auth_dc_migration",
                ) from None
            emit("auth_dc_redirect", operation=operation_name, attempt=attempt)


async def _auth_request_once(operation, operation_name):
    """Do not retry code requests or ambiguous login responses automatically."""
    emit("auth_request", operation=operation_name)
    try:
        return await asyncio.wait_for(operation(), timeout=45)
    except errors.FloodWaitError as exc:
        raise RecoveryError(
            f"Telegram pediu espera de {exc.seconds}s. Encerrei sem reenviar; repita auth depois.",
            code="auth_flood_wait",
        ) from None
    except (OSError, TimeoutError, errors.ServerError):
        raise RecoveryError(
            "Falha de rede/timeout na autenticação. Nenhum envio foi repetido automaticamente; "
            "verifique a conexão e execute auth depois.",
            code="auth_network",
        ) from None
    except errors.ApiIdInvalidError:
        raise RecoveryError(
            "Telegram recusou o API ID/API hash. Revise a configuração local.",
            code="credentials_invalid",
        ) from None
    except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError):
        raise RecoveryError(
            "Telegram recusou o telefone informado. Verifique-o no cliente oficial.",
            code="auth_phone_invalid",
        ) from None
    except errors.PhoneNumberUnoccupiedError:
        raise RecoveryError(
            "É necessária uma conta já cadastrada no Telegram; nenhuma conta foi criada.",
            code="auth_account_missing",
        ) from None


async def login_with_code(client):
    phone = prompt_validated(
        "phone",
        "Telefone internacional com + e DDI (visível neste terminal): ",
        lambda value: re.fullmatch(r"\+[1-9][0-9]{6,14}", value),
        normalize=lambda value: re.sub(r"[ ()-]", "", value.strip()),
    )
    sent = await auth_request(lambda: client.send_code_request(phone), "auth_send_code")
    code_hash = getattr(sent, "phone_code_hash", None)
    if not code_hash:
        raise RecoveryError(
            "Telegram retornou um fluxo de login não suportado. Verifique o cliente oficial.",
            code="auth_flow_unsupported",
        )
    emit("auth_step", stage="code_requested")
    print(
        "Código solicitado. Consulte o Telegram e digite-o somente neste terminal.", file=sys.stderr
    )
    for attempt in range(1, ATTEMPTS + 1):
        code = prompt_validated(
            "code",
            "Código de login (entrada oculta): ",
            lambda value: re.fullmatch(r"[0-9]{4,10}", value),
        )
        try:
            return await auth_request(
                lambda code=code: client.sign_in(phone=phone, code=code, phone_code_hash=code_hash),
                "auth_sign_in",
            )
        except errors.SessionPasswordNeededError:
            return await login_with_password(client)
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            emit("auth_retry", field="code", attempt=attempt)
            print("Código recusado. Digite novamente o código recebido.", file=sys.stderr)
        except errors.PhoneCodeExpiredError:
            raise RecoveryError(
                "Código expirado. Nenhum reenvio automático; execute auth novamente.",
                code="auth_code_expired",
            ) from None
    raise RecoveryError("Limite de tentativas de código atingido.", code="auth_attempts_exhausted")


async def login_with_password(client):
    emit("auth_step", stage="password_required")
    for attempt in range(1, ATTEMPTS + 1):
        password = prompt_validated(
            "password", "Senha 2FA (entrada oculta): ", bool, normalize=lambda value: value
        )
        try:
            return await auth_request(
                lambda password=password: client.sign_in(password=password), "auth_password"
            )
        except errors.PasswordHashInvalidError:
            emit("auth_retry", field="password", attempt=attempt)
            print("Senha 2FA recusada. Tente novamente.", file=sys.stderr)
    raise RecoveryError(
        "Limite de tentativas de senha 2FA atingido.", code="auth_attempts_exhausted"
    )


async def authenticate_user(root: Path, *, configure=False):
    require_terminal()
    session_dir = root / "sessions"
    session_path = session_dir / "telegram-recovery.session"
    with exclusive_lock(session_dir / ".inventory.lock"):
        credentials = (
            configure_credentials(root) if configure else telegram_client.load_credentials(root)
        )
        for file in session_dir.glob("*.session*"):
            private_file(file)
        fd = os.open(session_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        client = telegram_client.create_client(session_path, credentials)
        try:
            await auth_request(client.connect, "connect")
            emit("auth_step", stage="connected")
            authorized = await auth_request(client.is_user_authorized, "authorization")
            if authorized:
                user = await auth_request(client.get_me, "user")
            else:
                user = await login_with_code(client)
            if user is None or getattr(user, "bot", False):
                raise RecoveryError(
                    "É necessária uma sessão de conta de usuário, não de bot.", code="bot_session"
                )
        finally:
            try:
                await client.disconnect()
            finally:
                for file in session_dir.glob("*.session*"):
                    private_file(file)
        emit("auth_step", stage="already_authorized" if authorized else "authorized")
        print("Sessão de usuário autenticada e salva localmente.")
