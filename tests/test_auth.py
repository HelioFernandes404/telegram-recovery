import asyncio
import json
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from dotenv import dotenv_values
from telethon import errors

from telegram_recovery import auth, cli, telegram_client
from telegram_recovery.run_logging import RunLog
from telegram_recovery.security import RecoveryError, exclusive_lock

PHONE = "+5511987654321"
CODE = "9753186420"
PASSWORD = "  PRIVATE_2FA_PASSWORD  "
API_HASH = "abcdef0123456789abcdef0123456789"


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(auth, "require_terminal", lambda: None)
    monkeypatch.setattr(cli, "require_terminal", lambda: None)
    for key in auth.API_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def credentials(tmp_path):
    (tmp_path / ".env").write_text(f"TELEGRAM_API_ID=9999\nTELEGRAM_API_HASH={API_HASH}\n")


@pytest.fixture
def client(monkeypatch):
    # Existing scripted inputs include the phone first; keep all values simulated.
    monkeypatch.setattr(auth, "phone_input", lambda prompt: auth.secret_input(prompt))
    user = SimpleNamespace(bot=False, phone=PHONE, first_name="PRIVATE_NAME")
    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        get_me=AsyncMock(return_value=user),
        send_code_request=AsyncMock(
            return_value=SimpleNamespace(phone_code_hash="PRIVATE_CODE_HASH")
        ),
        sign_in=AsyncMock(return_value=user),
    )
    monkeypatch.setattr(telegram_client, "create_client", Mock(return_value=client))
    return client


def test_noninteractive_auth_never_prompts_or_connects(tmp_path, monkeypatch):
    prompt = Mock(side_effect=AssertionError("No prompts without a terminal"))
    factory = Mock(side_effect=AssertionError("No client without a terminal"))
    monkeypatch.setattr(auth, "secret_input", prompt)
    monkeypatch.setattr(telegram_client, "create_client", factory)
    assert cli.main(["auth", "--configure", "--root", str(tmp_path)]) == 1
    prompt.assert_not_called()
    factory.assert_not_called()
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "sessions").exists()


def test_getpass_fallback_is_refused(terminal, monkeypatch):
    def unsafe(*args, **kwargs):
        warnings.warn("Cannot disable echo", auth.getpass.GetPassWarning, stacklevel=1)
        pytest.fail("getpass warning should have raised before input")

    monkeypatch.setattr(auth.getpass, "getpass", unsafe)
    with pytest.raises(RecoveryError) as exc:
        auth.secret_input("Private: ")
    assert exc.value.code == "auth_terminal_required"


def test_eof_is_safe(terminal, monkeypatch):
    monkeypatch.setattr(auth.getpass, "getpass", Mock(side_effect=EOFError()))
    with pytest.raises(RecoveryError, match="cancelada"):
        auth.secret_input("Private: ")


def test_phone_uses_visible_local_input_but_code_remains_hidden(terminal, monkeypatch):
    visible = Mock(return_value=PHONE)
    hidden = Mock(return_value=CODE)
    monkeypatch.setattr("builtins.input", visible)
    monkeypatch.setattr(auth, "secret_input", hidden)
    assert auth.prompt_validated("phone", "Telefone: ", bool) == PHONE
    visible.assert_called_once_with("Telefone: ")
    hidden.assert_not_called()
    assert auth.prompt_validated("code", "Código: ", bool) == CODE
    hidden.assert_called_once_with("Código: ")
    assert visible.call_count == 1


def test_configure_preserves_content_and_writes_private_file(tmp_path, terminal, monkeypatch):
    original = b"# existing comment\r\nUNRELATED=keep\r\nTELEGRAM_API_ID=\r\nTELEGRAM_API_HASH="
    (tmp_path / ".env").write_bytes(original)
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=["9999", API_HASH]))
    result = auth.configure_credentials(tmp_path)
    assert result.api_id == 9999 and result.api_hash == API_HASH
    assert (tmp_path / ".env").read_bytes().startswith(original)
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert dotenv_values(tmp_path / ".env")["UNRELATED"] == "keep"
    assert not list(tmp_path.glob(".env.auth-*"))


def test_configure_reuses_existing_credentials_without_prompt(
    tmp_path, terminal, credentials, monkeypatch
):
    original = (tmp_path / ".env").read_bytes()
    prompt = Mock(side_effect=AssertionError("Already configured"))
    monkeypatch.setattr(auth, "secret_input", prompt)
    assert auth.configure_credentials(tmp_path).api_id == 9999
    prompt.assert_not_called()
    assert (tmp_path / ".env").read_bytes() == original


def test_configure_only_fills_missing_field(tmp_path, terminal, monkeypatch):
    (tmp_path / ".env").write_text("TELEGRAM_API_ID=9999\n")
    prompt = Mock(return_value=API_HASH)
    monkeypatch.setattr(auth, "secret_input", prompt)
    auth.configure_credentials(tmp_path)
    prompt.assert_called_once()
    assert "TELEGRAM_API_HASH" in prompt.call_args.args[0]


def test_invalid_environment_does_not_write_env(tmp_path, terminal, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    prompt = Mock(side_effect=AssertionError("Do not prompt for environment overrides"))
    monkeypatch.setattr(auth, "secret_input", prompt)
    with pytest.raises(RecoveryError, match="ambiente"):
        auth.configure_credentials(tmp_path)
    assert not (tmp_path / ".env").exists()


def test_existing_invalid_value_is_preserved(tmp_path, terminal, monkeypatch):
    original = "TELEGRAM_API_ID=PRIVATE_INVALID\n"
    (tmp_path / ".env").write_text(original)
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=AssertionError("Do not replace")))
    with pytest.raises(RecoveryError):
        auth.configure_credentials(tmp_path)
    assert (tmp_path / ".env").read_text() == original


def test_configure_does_not_overwrite_concurrent_edit(tmp_path, terminal, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# original\n")
    prompts = iter(["9999", API_HASH])

    def edited(prompt):
        path.write_text("# edited in user editor\n")
        return next(prompts)

    monkeypatch.setattr(auth, "secret_input", edited)
    with pytest.raises(RecoveryError) as exc:
        auth.configure_credentials(tmp_path)
    assert exc.value.code == "auth_config_changed"
    assert path.read_text() == "# edited in user editor\n"
    assert not list(tmp_path.glob(".env.auth-*"))


def test_auth_configure_and_code_success_have_no_secret_output(
    tmp_path, terminal, client, monkeypatch, capsys
):
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=["9999", API_HASH, PHONE, CODE]))
    assert cli.main(["auth", "--configure", "--root", str(tmp_path)]) == 0
    client.send_code_request.assert_awaited_once_with(PHONE)
    client.sign_in.assert_awaited_once_with(
        phone=PHONE, code=CODE, phone_code_hash="PRIVATE_CODE_HASH"
    )
    client.disconnect.assert_awaited_once()
    session = tmp_path / "sessions/telegram-recovery.session"
    assert session.stat().st_mode & 0o777 == 0o600
    assert session.parent.stat().st_mode & 0o777 == 0o700
    (path,) = (tmp_path / "logs").glob("auth-*.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["event"] == "auth_started"
    assert rows[-1]["status"] == "success"
    assert any(row.get("stage") == "authorized" for row in rows)
    output = capsys.readouterr()
    for secret in (PHONE, CODE, API_HASH, "PRIVATE_CODE_HASH", "PRIVATE_NAME"):
        assert secret not in output.out + output.err + path.read_text()
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "downloads").exists()


def test_two_factor_flow_preserves_password_whitespace(
    tmp_path, terminal, credentials, client, monkeypatch, capsys
):
    user = client.get_me.return_value
    client.sign_in.side_effect = [errors.SessionPasswordNeededError(None), user]
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=[PHONE, CODE, PASSWORD]))
    with RunLog(tmp_path, command="auth"):
        asyncio.run(auth.authenticate_user(tmp_path))
    assert client.sign_in.await_args_list[-1].kwargs == {"password": PASSWORD}
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err


def test_already_authorized_does_not_request_code(
    tmp_path, terminal, credentials, client, monkeypatch
):
    client.is_user_authorized.return_value = True
    prompt = Mock(side_effect=AssertionError("No new login for authorized session"))
    monkeypatch.setattr(auth, "secret_input", prompt)
    asyncio.run(auth.authenticate_user(tmp_path))
    prompt.assert_not_called()
    client.send_code_request.assert_not_awaited()
    client.sign_in.assert_not_awaited()


def test_bot_session_is_rejected_preserved_and_disconnected(
    tmp_path, terminal, credentials, client
):
    client.is_user_authorized.return_value = True
    client.get_me.return_value.bot = True
    with pytest.raises(RecoveryError) as exc:
        asyncio.run(auth.authenticate_user(tmp_path))
    assert exc.value.code == "bot_session"
    assert (tmp_path / "sessions/telegram-recovery.session").exists()
    client.disconnect.assert_awaited_once()


def test_invalid_code_retries_without_resending(
    tmp_path, terminal, credentials, client, monkeypatch
):
    client.sign_in.side_effect = [errors.PhoneCodeInvalidError(None), client.get_me.return_value]
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=[PHONE, CODE, CODE]))
    asyncio.run(auth.authenticate_user(tmp_path))
    client.send_code_request.assert_awaited_once()
    assert client.sign_in.await_count == 2


def test_password_retries_are_bounded(tmp_path, terminal, credentials, client, monkeypatch):
    client.sign_in.side_effect = [errors.SessionPasswordNeededError(None)] + [
        errors.PasswordHashInvalidError(None)
    ] * 3
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=[PHONE, CODE] + [PASSWORD] * 3))
    with pytest.raises(RecoveryError) as exc:
        asyncio.run(auth.authenticate_user(tmp_path))
    assert exc.value.code == "auth_attempts_exhausted"
    assert client.sign_in.await_count == 4
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    "failure,expected_code",
    [
        (errors.PhoneCodeExpiredError(None), "auth_code_expired"),
        (errors.FloodWaitError(None, 500), "auth_flood_wait"),
        (TimeoutError("PRIVATE_DETAILS"), "auth_network"),
        (errors.ApiIdInvalidError(None), "credentials_invalid"),
        (errors.PhoneNumberUnoccupiedError(None), "auth_account_missing"),
    ],
)
def test_login_errors_never_resend_or_leak(
    tmp_path, terminal, credentials, client, monkeypatch, failure, expected_code
):
    client.sign_in.side_effect = failure
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=[PHONE, CODE]))
    with pytest.raises(RecoveryError) as exc:
        asyncio.run(auth.authenticate_user(tmp_path))
    assert exc.value.code == expected_code
    assert "PRIVATE_DETAILS" not in str(exc.value)
    client.send_code_request.assert_awaited_once()
    client.sign_in.assert_awaited_once()
    client.disconnect.assert_awaited_once()


def test_auth_shares_inventory_lock(tmp_path, terminal, credentials, client):
    with exclusive_lock(tmp_path / "sessions/.inventory.lock"):
        with pytest.raises(RecoveryError) as exc:
            asyncio.run(auth.authenticate_user(tmp_path))
    assert exc.value.code == "already_running"
    client.connect.assert_not_awaited()


def test_raw_auth_errors_are_sanitized(
    tmp_path, terminal, credentials, client, monkeypatch, capsys
):
    client.send_code_request.side_effect = RuntimeError("PRIVATE_PROVIDER_TEXT")
    monkeypatch.setattr(auth, "secret_input", Mock(return_value=PHONE))
    assert cli.main(["auth", "--root", str(tmp_path)]) == 1
    output = capsys.readouterr()
    log = next((tmp_path / "logs").glob("auth-*.jsonl")).read_text()
    assert "PRIVATE_PROVIDER_TEXT" not in output.out + output.err + log


def test_keyboard_interrupt_closes_client(tmp_path, terminal, credentials, client, monkeypatch):
    monkeypatch.setattr(auth, "secret_input", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(auth.authenticate_user(tmp_path))
    client.disconnect.assert_awaited_once()


def test_invalid_phone_does_not_accept_bot_token(
    tmp_path, terminal, credentials, client, monkeypatch
):
    monkeypatch.setattr(auth, "secret_input", Mock(return_value="123456:PRIVATE_BOT_TOKEN"))
    with pytest.raises(RecoveryError):
        asyncio.run(auth.authenticate_user(tmp_path))
    client.send_code_request.assert_not_awaited()
    client.sign_in.assert_not_awaited()


@pytest.mark.parametrize(
    "error_type",
    [
        errors.PhoneMigrateError,
        errors.NetworkMigrateError,
        errors.UserMigrateError,
    ],
)
def test_auth_follows_one_definitive_dc_redirect(error_type):
    operation = AsyncMock(side_effect=[error_type(None, 2), "accepted"])
    assert asyncio.run(auth.auth_request(operation, "auth_send_code")) == "accepted"
    assert operation.await_count == 2


def test_auth_dc_redirect_budget_is_bounded():
    operation = AsyncMock(side_effect=errors.PhoneMigrateError(None, 2))
    with pytest.raises(RecoveryError) as exc:
        asyncio.run(auth.auth_request(operation, "auth_send_code"))
    assert exc.value.code == "auth_dc_migration"
    assert operation.await_count == 2


def test_telethon_routing_errors_are_not_hidden_as_value_errors(monkeypatch):
    from telethon import TelegramClient, functions, types
    from telethon.sessions import MemorySession

    async def scenario():
        # Use the actual Telethon request loop with a fake sender, without connect().
        client = TelegramClient(
            MemorySession(),
            1,
            "0" * 32,
            request_retries=0,
            raise_last_call_error=True,
        )
        client.is_user_authorized = AsyncMock(return_value=False)
        client._switch_dc = AsyncMock()
        response = types.auth.SentCode(
            type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="TEST_HASH"
        )
        outcomes = iter([errors.PhoneMigrateError(None, 2), response])

        def send(request, **kwargs):
            future = asyncio.get_running_loop().create_future()
            result = next(outcomes)
            if isinstance(result, Exception):
                future.set_exception(result)
            else:
                future.set_result(result)
            return future

        sender = SimpleNamespace(send=send)
        request = functions.auth.SendCodeRequest(PHONE, 1, "0" * 32, types.CodeSettings())
        result = await auth.auth_request(lambda: client._call(sender, request), "auth_send_code")
        assert result is response
        client._switch_dc.assert_awaited_once_with(2)

    asyncio.run(scenario())
