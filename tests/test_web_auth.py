import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors

from telegram_recovery import telegram_client
from telegram_recovery.web_auth import AuthFlow

PHONE = "+5511987654321"
CODE = "9753186420"
PASSWORD = "  PRIVATE_2FA_PASSWORD  "
API_HASH = "abcdef0123456789abcdef0123456789"


def wait_for_phase(flow, phase, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = flow.status()
        if state["phase"] == phase:
            return state
        time.sleep(0.01)
    pytest.fail(f"phase did not become {phase!r}: {flow.status()!r}")


def wait_for_await(mock, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mock.await_count:
            return
        time.sleep(0.01)
    pytest.fail("mock was not awaited before timeout")


@pytest.fixture
def configured_root(tmp_path):
    (tmp_path / ".env").write_text(f"TELEGRAM_API_ID=9999\nTELEGRAM_API_HASH={API_HASH}\n")
    return tmp_path


@pytest.fixture
def fake_client(monkeypatch):
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


def test_web_flow_starts_at_phone_when_credentials_exist(configured_root):
    flow = AuthFlow(configured_root)
    try:
        state = flow.status()
        assert state["phase"] == "phone"
        assert state["credentials_configured"] is True
    finally:
        flow.close()


def test_web_flow_completes_code_login_without_secret_output(configured_root, fake_client):
    flow = AuthFlow(configured_root)
    try:
        assert flow.begin(PHONE)["phase"] == "code"
        flow.submit_code(CODE)
        assert wait_for_phase(flow, "success")["phase"] == "success"
        fake_client.send_code_request.assert_awaited_once_with(PHONE)
        fake_client.sign_in.assert_awaited_once_with(
            phone=PHONE, code=CODE, phone_code_hash="PRIVATE_CODE_HASH"
        )
        wait_for_await(fake_client.disconnect)
        fake_client.disconnect.assert_awaited_once()
        session = configured_root / "sessions/telegram-recovery.session"
        assert session.exists()
        log_text = next((configured_root / "logs").glob("auth-*.jsonl")).read_text()
        assert PHONE not in log_text
        assert CODE not in log_text
        assert "PRIVATE_CODE_HASH" not in log_text
    finally:
        flow.close()


def test_web_flow_guides_two_factor_without_trimming_password(configured_root, fake_client):
    user = fake_client.get_me.return_value
    fake_client.sign_in.side_effect = [errors.SessionPasswordNeededError(None), user]
    flow = AuthFlow(configured_root)
    try:
        flow.begin(PHONE)
        flow.submit_code(CODE)
        wait_for_phase(flow, "password")
        flow.submit_password(PASSWORD)
        assert wait_for_phase(flow, "success")["phase"] == "success"
        assert fake_client.sign_in.await_args_list[-1].kwargs == {"password": PASSWORD}
        log_text = next((configured_root / "logs").glob("auth-*.jsonl")).read_text()
        assert PASSWORD not in log_text
    finally:
        flow.close()
