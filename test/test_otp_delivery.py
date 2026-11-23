import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from services import email as mail
from routers import auth


@pytest.fixture
def transports(monkeypatch):
    for key in ('RESEND_API_KEY', 'SENDGRID_API_KEY', 'SMTP_USER', 'SMTP_PASS'):
        monkeypatch.setattr(mail, key, '')


def test_otp_requires_configured_transport(transports):
    with pytest.raises(mail.EmailDeliveryError):
        mail.send_otp_email.run('user@example.com', 'Test User', '123456')


def test_failed_providers_raise_for_otp_without_logging_code(transports, monkeypatch, caplog):
    monkeypatch.setattr(mail, 'RESEND_API_KEY', 're_test')
    with patch.object(mail, '_send_via_resend', side_effect=RuntimeError('provider rejected request')):
        with pytest.raises(mail.EmailDeliveryError):
            mail.send_otp_email.run('user@example.com', 'Test User', '123456')
    assert '123456' not in caplog.text


def test_provider_fallback_accepts_otp(transports, monkeypatch):
    monkeypatch.setattr(mail, 'RESEND_API_KEY', 're_test')
    monkeypatch.setattr(mail, 'SENDGRID_API_KEY', 'SG.test')
    with patch.object(mail, '_send_via_resend', side_effect=RuntimeError('unavailable')), patch.object(mail, '_send_via_sendgrid') as fallback:
        mail.send_otp_email.run('user@example.com', 'Test User', '123456')
    fallback.assert_called_once()


def test_placeholders_do_not_count_as_transports(transports, monkeypatch):
    monkeypatch.setattr(mail, 'RESEND_API_KEY', 're_your-resend-api-key')
    monkeypatch.setattr(mail, 'SENDGRID_API_KEY', 'your-sendgrid-api-key')
    monkeypatch.setattr(mail, 'SMTP_USER', 'your-email@gmail.com')
    monkeypatch.setattr(mail, 'SMTP_PASS', 'your-app-password')
    with pytest.raises(mail.EmailDeliveryError, match='No email transport'):
        mail.send_email('user@example.com', 'Test', '<p>Test</p>', raise_on_failure=True)


def test_ordinary_notifications_still_fail_safely(transports):
    mail.send_email('user@example.com', 'Test', '<p>Test</p>')


def test_password_reset_requires_acceptance(transports):
    with pytest.raises(mail.EmailDeliveryError):
        mail.send_password_reset_email.run('user@example.com', 'User', '123456')


@pytest.fixture
def profile_db():
    database = MagicMock()
    database.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {'id': 'user-1', 'full_name': 'Test User'}
    ]
    with patch.object(auth, 'supabase_admin', database):
        yield database


def test_send_otp_reports_delivery_failure(profile_db):
    with patch.object(auth.send_otp_email, 'run', side_effect=mail.EmailDeliveryError('failed')):
        with pytest.raises(HTTPException) as failure:
            asyncio.run(auth.send_otp(auth.SendOtpRequest(email='user@example.com')))
    assert failure.value.status_code == 503
    assert 'failed' not in failure.value.detail


def test_send_otp_waits_for_provider_without_returning_code(profile_db):
    with patch.object(auth.send_otp_email, 'run') as send, patch.object(auth.send_otp_email, 'delay') as queue:
        response = asyncio.run(auth.send_otp(auth.SendOtpRequest(email='user@example.com')))
    send.assert_called_once()
    queue.assert_not_called()
    assert 'otp_code' not in response
    assert 'accepted for delivery' in response['message']
    code = send.call_args.args[2]
    stored = profile_db.table.return_value.update.call_args.args[0]
    assert stored['otp_code'] == code
    assert len(code) == 6 and code.isdigit()
