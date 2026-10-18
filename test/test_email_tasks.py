import pytest
from unittest.mock import patch, MagicMock
from services import email as email_service


def test_email_functions_are_celery_tasks():
    """Verify all 15 transactional email sending functions + digest are decorated as Celery tasks."""
    task_functions = [
        email_service.send_welcome_customer,
        email_service.send_welcome_vendor,
        email_service.send_admin_new_vendor,
        email_service.send_vendor_approved,
        email_service.send_vendor_suspended,
        email_service.send_order_confirmation,
        email_service.send_new_sale_alert,
        email_service.send_order_in_transit,
        email_service.send_order_delivered,
        email_service.send_order_cancelled,
        email_service.send_wallet_topup_receipt,
        email_service.send_product_submitted,
        email_service.send_product_approved,
        email_service.send_product_rejected,
        email_service.send_weekly_vendor_digest,
    ]

    for func in task_functions:
        assert hasattr(func, "delay"), f"{func.__name__} is not a Celery task (missing .delay)"
        assert hasattr(func, "apply_async"), f"{func.__name__} is not a Celery task (missing .apply_async)"


def test_task_delay_queues_async():
    """Verify calling .delay() offloads execution asynchronously to Celery message queue."""
    with patch.object(email_service.send_welcome_customer, "apply_async") as mock_apply:
        email_service.send_welcome_customer.delay("user@example.com", "John Doe")
        assert mock_apply.called
        assert mock_apply.call_args[0][0] == ("user@example.com", "John Doe")


def test_task_execution_calls_send_email():
    """Verify running task directly invokes underlying email transport."""
    with patch("services.email.send_email") as mock_send:
        email_service.send_welcome_customer.run("customer@example.com", "Alice")
        assert mock_send.called
        assert mock_send.call_args[0][0] == "customer@example.com"
