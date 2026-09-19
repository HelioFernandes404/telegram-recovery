import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors

from telegram_recovery import telegram_client
from telegram_recovery.security import RecoveryError, exclusive_lock
from telegram_recovery.telegram_client import (
    RetryPolicy,
    channel_number,
    existing_user_session,
    load_credentials,
    read_with_retry,
    resolve_channel,
)


@pytest.fixture
def fake_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "a" * 32)


def test_env_precedence_no_interpolation_and_no_secret_repr(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    (tmp_path / ".env").write_text("TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=" + "a" * 32)
    credentials = load_credentials(tmp_path)
    assert credentials.api_id == 12345
    assert "12345" not in repr(credentials) and "a" * 32 not in repr(credentials)
    monkeypatch.setenv("TELEGRAM_API_ID", "54321")
    assert load_credentials(tmp_path).api_id == 54321
    monkeypatch.setenv("TELEGRAM_API_HASH", "")
    with pytest.raises(RecoveryError):
        load_credentials(tmp_path)
    monkeypatch.delenv("TELEGRAM_API_HASH")
    monkeypatch.setenv("OTHER_SECRET", "b" * 32)
    (tmp_path / ".env").write_text("TELEGRAM_API_ID=1\nTELEGRAM_API_HASH=${OTHER_SECRET}")
    with pytest.raises(RecoveryError):
        load_credentials(tmp_path)


def test_missing_session_never_constructs_client_or_prompts(tmp_path, monkeypatch):
    factory = Mock(side_effect=AssertionError("Must not construct a client"))
    monkeypatch.setattr(telegram_client, "TelegramClient", factory)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("Must not prompt")))

    async def check():
        async with existing_user_session(tmp_path):
            pytest.fail("Missing session was accepted")

    with pytest.raises(RecoveryError, match="Sessão local ausente"):
        asyncio.run(check())
    factory.assert_not_called()
    assert not (tmp_path / "sessions" / "telegram-recovery.session").exists()


@pytest.mark.parametrize(
    "authorized,bot,expected_error",
    [
        (True, False, None),
        (False, False, "Sessão não autenticada"),
        (True, True, "conta de usuário"),
    ],
)
def test_session_requires_user_and_disconnects(
    tmp_path, monkeypatch, fake_credentials, authorized, bot, expected_error
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_file = session_dir / "telegram-recovery.session"
    session_file.touch(mode=0o644)
    # This fake deliberately has no auth/start/login/download/mutation methods.
    fake = SimpleNamespace(
        connect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=authorized),
        get_me=AsyncMock(return_value=SimpleNamespace(bot=bot)),
        disconnect=AsyncMock(),
    )
    factory = Mock(return_value=fake)
    monkeypatch.setattr(telegram_client, "TelegramClient", factory)

    async def check():
        async with existing_user_session(tmp_path) as client:
            assert client is fake

    if expected_error:
        with pytest.raises(RecoveryError, match=expected_error):
            asyncio.run(check())
    else:
        asyncio.run(check())
    fake.disconnect.assert_awaited_once()
    assert factory.call_args.kwargs["receive_updates"] is False
    assert factory.call_args.kwargs["flood_sleep_threshold"] == 0
    assert factory.call_args.kwargs["raise_last_call_error"] is True
    assert session_dir.stat().st_mode & 0o777 == 0o700
    assert session_file.stat().st_mode & 0o777 == 0o600


def test_retry_transient_network_and_floodwait(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_client.asyncio, "sleep", sleep)
    operation = AsyncMock(side_effect=[TimeoutError("secret"), errors.FloodWaitError(None, 3), 42])
    progress = []
    assert asyncio.run(read_with_retry(operation, progress=progress.append)) == 42
    assert [call.args[0] for call in sleep.await_args_list] == [1, 3]
    assert "secret" not in " ".join(progress)


def test_floodwait_above_ceiling_stops_without_sleep(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_client.asyncio, "sleep", sleep)
    operation = AsyncMock(side_effect=errors.FloodWaitError(None, 301))
    with pytest.raises(RecoveryError, match="301s"):
        asyncio.run(read_with_retry(operation))
    sleep.assert_not_awaited()


def test_retry_budget_is_finite(monkeypatch):
    monkeypatch.setattr(telegram_client.asyncio, "sleep", AsyncMock())
    operation = AsyncMock(side_effect=OSError("private connection details"))
    with pytest.raises(RecoveryError) as exc:
        asyncio.run(read_with_retry(operation, policy=RetryPolicy(attempts=2)))
    assert operation.await_count == 2
    assert "private" not in str(exc.value)


def test_resolve_channel_cache_and_dialog_fallback():
    channel = -1001234567890
    fake = SimpleNamespace(get_input_entity=AsyncMock(return_value="cached"))
    assert asyncio.run(resolve_channel(fake, channel)) == "cached"
    fake.get_input_entity = AsyncMock(side_effect=ValueError("not cached"))

    async def dialogs():
        yield SimpleNamespace(id=-1001234567892, input_entity="other")
        yield SimpleNamespace(id=channel, input_entity="target")

    fake.iter_dialogs = dialogs
    assert asyncio.run(resolve_channel(fake, channel)) == "target"


def test_channel_validation():
    assert channel_number("-1001234567890") == -1001234567890
    for bad in ("2228510001", "-12345", "a name", "-1000000000000"):
        with pytest.raises(ValueError):
            channel_number(bad)


def test_lock_blocks_second_writer_and_releases(tmp_path):
    path = tmp_path / "inventory.lock"
    with exclusive_lock(path):
        with pytest.raises(RecoveryError, match="Outra operação"):
            with exclusive_lock(path):
                pytest.fail("Second lock succeeded")
    with exclusive_lock(path):
        pass
