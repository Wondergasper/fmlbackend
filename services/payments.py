import os
import json
import hmac
import hashlib
import logging
import urllib.request
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

PAYSTACK_SECRET_KEY = settings.paystack_secret_key
PAYSTACK_WEBHOOK_SECRET = os.getenv("PAYSTACK_WEBHOOK_SECRET", "")
PAYSTACK_CALLBACK_URL = os.getenv("PAYSTACK_CALLBACK_URL", "")
PAYSTACK_INITIALIZE_URL = "https://api.paystack.co/transaction/initialize"
PAYSTACK_VERIFY_URL = "https://api.paystack.co/transaction/verify/{}"


def _paystack_headers() -> dict[str, str]:
    if not PAYSTACK_SECRET_KEY:
        raise RuntimeError("Missing Paystack secret key. Set PAYSTACK_SECRET_KEY in environment.")
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def initialize_paystack_transaction(email: str, amount_kobo: int, user_id: str) -> dict[str, Any]:
    payload = {
        "email": email,
        "amount": amount_kobo,
        "currency": "NGN",
        "metadata": {
            "user_id": user_id,
            "purpose": "wallet_topup",
        },
    }
    if PAYSTACK_CALLBACK_URL:
        payload["callback_url"] = PAYSTACK_CALLBACK_URL

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        PAYSTACK_INITIALIZE_URL,
        data=data,
        headers=_paystack_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        result = json.loads(body)
        if not result.get("status"):
            raise RuntimeError(f"Paystack init failed: {result.get('message')}")
        return result["data"]


def verify_paystack_transaction(reference: str) -> dict[str, Any]:
    if not PAYSTACK_SECRET_KEY:
        raise RuntimeError("Missing Paystack secret key. Set PAYSTACK_SECRET_KEY in environment.")

    url = PAYSTACK_VERIFY_URL.format(reference)
    req = urllib.request.Request(url, headers=_paystack_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        result = json.loads(body)
        if not result.get("status"):
            raise RuntimeError(f"Paystack verify failed: {result.get('message')}")
        return result["data"]


def verify_paystack_webhook_signature(signature: str, body: bytes) -> bool:
    if not PAYSTACK_WEBHOOK_SECRET:
        logger.warning("Paystack webhook secret not configured; rejecting webhook.")
        return False
    computed = hmac.new(
        PAYSTACK_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)
