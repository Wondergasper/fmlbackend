"""
services/email.py — Farm-Connect Email Notification Service

Sends transactional emails via Resend.ai (primary) with SendGrid and SMTP fallbacks.
All functions are fire-and-forget safe: errors are logged but never raised,
so a failed email never crashes the API response.

Environment variables required (.env):
  RESEND_API_KEY         — Resend.ai API key
  FROM_EMAIL             — Sender address, e.g. noreply@farmconnect.ng
  FROM_NAME              — Sender display name, e.g. Farm-Connect
  FRONTEND_URL           — Base URL for CTA buttons, e.g. https://farmconnect.ng
  SENDGRID_API_KEY       — SendGrid API key (optional fallback)

Optional SMTP fallback (used when SENDGRID_API_KEY is not set):
  SMTP_HOST               — e.g. smtp.gmail.com
  SMTP_PORT               — e.g. 587
  SMTP_USER               — Your Gmail / SMTP username
  SMTP_PASS               — App password

Supported notifications (15 total):
  Account  : welcome_customer, welcome_vendor, admin_new_vendor,
             vendor_approved, vendor_suspended
  Orders   : order_confirmation, new_sale_alert, order_in_transit,
             order_delivered, order_cancelled
  Wallet   : wallet_topup_receipt
  Products : product_submitted, product_approved, product_rejected
  Digest   : weekly_vendor_digest
"""

import os
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from celery_app import celery_app
from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RESEND_API_KEY  = settings.resend_api_key
RESEND_API_URL  = "https://api.resend.com/emails"
SENDGRID_API_KEY = settings.sendgrid_api_key
FROM_EMAIL       = os.getenv("FROM_EMAIL", "noreply@farmconnect.ng")
FROM_NAME        = os.getenv("FROM_NAME", "Farm-Connect")
FRONTEND_URL     = os.getenv("FRONTEND_URL", "http://localhost:5173")

SMTP_HOST = settings.smtp_host
SMTP_PORT = settings.smtp_port
SMTP_USER = settings.smtp_user
SMTP_PASS = settings.smtp_password

# Brand colours
COLOR_PRIMARY   = "#2D6A4F"   # deep forest green
COLOR_SECONDARY = "#52B788"   # light green
COLOR_ACCENT    = "#D8F3DC"   # pale green background
COLOR_DARK      = "#1B4332"   # header background
COLOR_TEXT      = "#2D3748"   # body text
COLOR_MUTED     = "#718096"   # secondary text
COLOR_ERROR     = "#C53030"   # red for warnings
COLOR_WHITE     = "#FFFFFF"


# ---------------------------------------------------------------------------
# Core HTML frame
# ---------------------------------------------------------------------------

def _base_html(title: str, body: str) -> str:
    """Wraps body HTML in a responsive email frame with Farm-Connect branding."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background-color:#F0FFF4;font-family:'Segoe UI',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background-color:#F0FFF4;padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width:600px;border-radius:16px;
               overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.10);">

          <!-- Header -->
          <tr>
            <td style="background-color:{COLOR_DARK};padding:28px 32px;text-align:center;">
              <span style="font-size:28px;">🌾</span>
              <h1 style="margin:8px 0 0;color:{COLOR_WHITE};font-size:22px;
                         font-weight:800;letter-spacing:-0.5px;">Farm-Connect</h1>
              <p style="margin:4px 0 0;color:rgba(255,255,255,0.6);font-size:12px;">
                Nigeria's Freshest Farmers Market
              </p>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background-color:{COLOR_WHITE};padding:36px 32px;">
              {body}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:{COLOR_ACCENT};padding:20px 32px;text-align:center;">
              <p style="margin:0;font-size:12px;color:{COLOR_MUTED};">
                © {datetime.now().year} Farm-Connect Nigeria · 
                <a href="{FRONTEND_URL}" style="color:{COLOR_PRIMARY};text-decoration:none;">
                  Visit our market
                </a>
              </p>
              <p style="margin:6px 0 0;font-size:11px;color:{COLOR_MUTED};">
                You received this email because you have an account on Farm-Connect.<br/>
                If you did not take this action, please 
                <a href="mailto:support@farmconnect.ng" style="color:{COLOR_PRIMARY};">
                  contact support
                </a>.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _btn(text: str, url: str, color: str = None) -> str:
    """Returns a styled HTML CTA button."""
    bg = color or COLOR_PRIMARY
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px auto 0;">
      <tr>
        <td style="border-radius:10px;background-color:{bg};">
          <a href="{url}" style="display:inline-block;padding:14px 28px;color:{COLOR_WHITE};
             font-size:15px;font-weight:700;text-decoration:none;border-radius:10px;">
            {text}
          </a>
        </td>
      </tr>
    </table>"""


def _info_row(label: str, value: str) -> str:
    """A single label-value row inside an info table."""
    return f"""
    <tr>
      <td style="padding:8px 0;font-size:13px;color:{COLOR_MUTED};
                 font-weight:600;white-space:nowrap;vertical-align:top;">
        {label}
      </td>
      <td style="padding:8px 0 8px 16px;font-size:14px;color:{COLOR_TEXT};
                 font-weight:700;vertical-align:top;">
        {value}
      </td>
    </tr>"""


def _info_table(*rows: str) -> str:
    """Wraps info rows in a styled container table."""
    inner = "".join(rows)
    return f"""
    <table role="presentation" width="100%"
           style="margin:20px 0;background-color:{COLOR_ACCENT};
                  border-radius:12px;padding:16px 20px;border-collapse:collapse;">
      {inner}
    </table>"""


def _divider() -> str:
    return f'<hr style="border:none;border-top:1px solid #E2E8F0;margin:24px 0;"/>'


def _badge(text: str, color: str = None) -> str:
    bg = color or COLOR_PRIMARY
    return (f'<span style="display:inline-block;background-color:{bg};color:{COLOR_WHITE};'
            f'font-size:12px;font-weight:700;padding:4px 10px;border-radius:20px;">{text}</span>')


def _naira(kobo: int) -> str:
    """Formats a kobo integer as ₦ Naira string."""
    return f"₦{kobo / 100:,.2f}"


# ---------------------------------------------------------------------------
# Transport layer
# ---------------------------------------------------------------------------

def _send_via_resend(to: str, subject: str, html: str) -> None:
    """Send via Resend.ai HTTP API."""
    import urllib.request, urllib.error, json
    payload = json.dumps({
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": [to],
        "subject": subject,
        "html": html,
    }).encode("utf-8")

    req = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Farm-Connect/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"Resend returned HTTP {resp.status}")


def _send_via_sendgrid(to: str, subject: str, html: str) -> None:
    """Send via SendGrid HTTP API (no sdk dependency — uses raw HTTP)."""
    import urllib.request, urllib.error, json
    payload = json.dumps({
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": FROM_EMAIL, "name": FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"SendGrid returned HTTP {resp.status}")


def _send_via_smtp(to: str, subject: str, html: str) -> None:
    """Send via SMTP (Gmail or any SMTP relay)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(FROM_EMAIL, to, msg.as_string())


def send_email(to: str, subject: str, html: str) -> None:
    """
    Send a transactional email. Tries Resend.ai first, falls back to SendGrid and SMTP.
    Errors are logged but never raised so the API response is never blocked.
    """
    if not to:
        logger.warning("[email] Skipped — no recipient address provided.")
        return
    try:
        if RESEND_API_KEY:
            _send_via_resend(to, subject, html)
            logger.info(f"[email] Resend ✓  to={to}  subject={subject!r}")
        elif SENDGRID_API_KEY:
            _send_via_sendgrid(to, subject, html)
            logger.info(f"[email] SendGrid ✓  to={to}  subject={subject!r}")
        elif SMTP_USER:
            _send_via_smtp(to, subject, html)
            logger.info(f"[email] SMTP ✓  to={to}  subject={subject!r}")
        else:
            logger.warning(f"[email] No transport configured — would send to={to}  subject={subject!r}")
    except Exception as exc:
        logger.error(f"[email] FAILED  to={to}  subject={subject!r}  error={exc}")


# ===========================================================================
# ⓪ OTP VERIFICATION
# ===========================================================================

@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_otp_email(email: str, name: str, otp_code: str) -> None:
    """#0 — Send email OTP verification code."""
    first = name.split()[0] if name else "there"
    body = f"""
    <h2 style="margin:0 0 8px;color:{COLOR_DARK};font-size:24px;font-weight:800;">
      Your verification code 🔐
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;line-height:1.6;">
      Hi {first}, use the code below to verify your Farm-Connect account.
      This code expires in <strong>10 minutes</strong>.
    </p>
    <div style="text-align:center;margin:32px 0;">
      <span style="display:inline-block;font-size:42px;font-weight:900;letter-spacing:12px;
                   font-family:'Courier New',monospace;color:{COLOR_DARK};
                   background-color:{COLOR_ACCENT};padding:20px 32px;border-radius:16px;">
        {otp_code}
      </span>
    </div>
    <p style="margin:0;font-size:13px;color:{COLOR_MUTED};text-align:center;">
      If you did not request this code, please ignore this email.
    </p>
    {_btn("Go to Farm-Connect →", FRONTEND_URL)}
    """
    send_email(email, f"Your Farm-Connect verification code: {otp_code}", _base_html("OTP Verification", body))


# ===========================================================================
# ① ACCOUNT NOTIFICATIONS
# ===========================================================================

@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_welcome_customer(email: str, name: str) -> None:
    """#1 — Welcome email for new customer accounts."""
    first = name.split()[0] if name else "there"
    body = f"""
    <h2 style="margin:0 0 8px;color:{COLOR_DARK};font-size:24px;font-weight:800;">
      Welcome to Farm-Connect, {first}! 🌾
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;line-height:1.6;">
      Your account is ready. You can now browse fresh produce sourced directly
      from verified Nigerian farmers — no middlemen, better prices, and farm-to-table
      freshness guaranteed.
    </p>
    {_divider()}
    <h3 style="margin:0 0 12px;color:{COLOR_TEXT};font-size:16px;">What you can do next:</h3>
    <ul style="margin:0 0 20px;padding-left:20px;color:{COLOR_TEXT};font-size:14px;line-height:2;">
      <li>🛒 Browse hundreds of fresh listings</li>
      <li>💰 Fund your wallet to place orders instantly</li>
      <li>📦 Track deliveries in real-time</li>
    </ul>
    {_btn("Browse the Market →", FRONTEND_URL + "/marketplace")}
    """
    send_email(email, f"Welcome to Farm-Connect, {first}! 🌾", _base_html("Welcome", body))


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_welcome_vendor(email: str, name: str) -> None:
    """#1b — Welcome email for new vendor accounts (pending approval)."""
    body = f"""
    <h2 style="margin:0 0 8px;color:{COLOR_DARK};font-size:24px;font-weight:800;">
      Welcome, {name}! Your vendor application is under review 🌱
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;line-height:1.6;">
      Thank you for registering as a vendor on Farm-Connect. Our team will review
      your application and respond within <strong>24 hours</strong>.
    </p>
    {_divider()}
    <h3 style="margin:0 0 12px;color:{COLOR_TEXT};font-size:16px;">While you wait, you can:</h3>
    <ul style="margin:0 0 20px;padding-left:20px;color:{COLOR_TEXT};font-size:14px;line-height:2;">
      <li>📝 Complete your farm profile and bio</li>
      <li>📸 Prepare product photos (JPEG, PNG, WebP — max 5 MB each)</li>
      <li>💰 Review our pricing guidelines</li>
    </ul>
    <p style="margin:0;font-size:13px;color:{COLOR_MUTED};">
      You'll receive another email as soon as your account is approved.
    </p>
    {_btn("Go to your dashboard →", FRONTEND_URL + "/vendor")}
    """
    send_email(email, "Farm-Connect — Your vendor application is under review", _base_html("Vendor Application", body))


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_admin_new_vendor(admin_email: str, vendor_name: str, vendor_email: str, vendor_id: str) -> None:
    """#2 — Alert admin when a new vendor registers."""
    body = f"""
    <h2 style="margin:0 0 8px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      New vendor application received
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      A new vendor has registered and is awaiting approval.
    </p>
    {_info_table(
        _info_row("Name:", vendor_name),
        _info_row("Email:", vendor_email),
        _info_row("Vendor ID:", f"<code>{vendor_id}</code>"),
        _info_row("Status:", _badge("Pending Approval", "#D97706")),
    )}
    {_btn("Review in Admin Dashboard →", FRONTEND_URL + "/admin/vendors")}
    """
    send_email(admin_email, f"New vendor application — {vendor_name}", _base_html("New Vendor", body))


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_vendor_approved(email: str, name: str) -> None:
    """#3 — Vendor account approved by admin."""
    body = f"""
    <h2 style="margin:0 0 8px;color:{COLOR_DARK};font-size:24px;font-weight:800;">
      Congratulations, {name}! Your vendor account is approved ✅
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;line-height:1.6;">
      Your Farm-Connect vendor account has been reviewed and approved. You can
      now list products and start receiving orders from customers across Nigeria.
    </p>
    {_divider()}
    <h3 style="margin:0 0 12px;color:{COLOR_TEXT};font-size:16px;">Get started in 3 steps:</h3>
    <ol style="margin:0 0 20px;padding-left:20px;color:{COLOR_TEXT};font-size:14px;line-height:2.2;">
      <li>Complete your <strong>bank details</strong> for payouts</li>
      <li>Add your first <strong>product listing</strong></li>
      <li>Start fulfilling <strong>orders</strong>!</li>
    </ol>
    {_btn("Go to your dashboard →", FRONTEND_URL + "/vendor")}
    """
    send_email(email, "Your Farm-Connect vendor account is approved! ✅", _base_html("Account Approved", body))


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_vendor_suspended(email: str, name: str, reason: Optional[str] = None) -> None:
    """#4 — Vendor account suspended by admin."""
    reason_block = f"""
    {_divider()}
    <p style="margin:0;font-size:14px;color:{COLOR_TEXT};">
      <strong>Reason given:</strong><br/>
      <span style="color:{COLOR_MUTED};">{reason}</span>
    </p>""" if reason else ""
    body = f"""
    <h2 style="margin:0 0 8px;color:{COLOR_ERROR};font-size:22px;font-weight:800;">
      Your vendor account has been suspended ⚠️
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;line-height:1.6;">
      Hi {name}, your Farm-Connect vendor account has been temporarily suspended.
      Your active listings have been hidden from the marketplace.
    </p>
    {reason_block}
    {_divider()}
    <p style="margin:0;font-size:14px;color:{COLOR_MUTED};">
      If you believe this is a mistake or would like to appeal, please reply to
      this email or contact 
      <a href="mailto:support@farmconnect.ng" style="color:{COLOR_PRIMARY};">
        support@farmconnect.ng
      </a>.
    </p>
    """
    send_email(email, "Important: Your Farm-Connect account has been suspended", _base_html("Account Suspended", body))


# ===========================================================================
# ② ORDER NOTIFICATIONS
# ===========================================================================

def _format_items(items: list) -> str:
    """Render a list of order items as HTML rows."""
    rows = ""
    for item in items:
        name = item.get("name") or item.get("product_name", "Product")
        qty  = item.get("quantity", 1)
        price = item.get("unit_price_kobo") or item.get("unit_price", 0)
        rows += f"""
        <tr>
          <td style="padding:8px 0;font-size:14px;color:{COLOR_TEXT};border-bottom:1px solid #EDF2F7;">
            {name}
          </td>
          <td style="padding:8px 0;font-size:14px;color:{COLOR_MUTED};
                     text-align:center;border-bottom:1px solid #EDF2F7;">
            ×{qty}
          </td>
          <td style="padding:8px 0;font-size:14px;color:{COLOR_TEXT};font-weight:700;
                     text-align:right;border-bottom:1px solid #EDF2F7;">
            {_naira(price * qty)}
          </td>
        </tr>"""
    return f"""
    <table role="presentation" width="100%" style="border-collapse:collapse;margin:12px 0;">
      <tr>
        <th style="font-size:12px;color:{COLOR_MUTED};text-align:left;
                   padding:0 0 8px;font-weight:600;border-bottom:2px solid {COLOR_ACCENT};">
          Item
        </th>
        <th style="font-size:12px;color:{COLOR_MUTED};text-align:center;
                   padding:0 0 8px;font-weight:600;border-bottom:2px solid {COLOR_ACCENT};">
          Qty
        </th>
        <th style="font-size:12px;color:{COLOR_MUTED};text-align:right;
                   padding:0 0 8px;font-weight:600;border-bottom:2px solid {COLOR_ACCENT};">
          Subtotal
        </th>
      </tr>
      {rows}
    </table>"""


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_order_confirmation(
    email: str,
    name: str,
    order_id: str,
    items: list,
    total_kobo: int,
    delivery_type: str,
    delivery_address: str,
    new_balance_kobo: int,
) -> None:
    """#5 — Order confirmation for customers."""
    first = name.split()[0] if name else "there"
    eta   = "1-2 business days" if delivery_type == "standard" else "Same day / Next day"
    body  = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      Order confirmed! 🛒
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {first}, your order has been placed successfully.
    </p>
    {_info_table(
        _info_row("Order ID:", f"<code>#{order_id[:8].upper()}</code>"),
        _info_row("Delivery:", delivery_type.title()),
        _info_row("Ship to:", delivery_address),
        _info_row("ETA:", eta),
    )}
    {_format_items(items)}
    {_info_table(
        _info_row("Order total:", _naira(total_kobo)),
        _info_row("Wallet balance:", _naira(new_balance_kobo) + " remaining"),
    )}
    {_btn("Track your order →", FRONTEND_URL + "/customer/orders/" + order_id)}
    """
    send_email(
        email,
        f"Order #{order_id[:8].upper()} confirmed — {_naira(total_kobo)}",
        _base_html("Order Confirmation", body),
    )


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_new_sale_alert(
    vendor_email: str,
    vendor_name: str,
    order_id: str,
    items: list,
    delivery_type: str,
    delivery_city: str,
) -> None:
    """#6 — New sale alert for vendors."""
    body = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      🛍️ New order received!
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {vendor_name}, a customer has purchased your product(s). Please prepare
      the items for dispatch.
    </p>
    {_info_table(
        _info_row("Order ID:", f"<code>#{order_id[:8].upper()}</code>"),
        _info_row("Delivery type:", delivery_type.title()),
        _info_row("Ship to:", delivery_city),
    )}
    {_format_items(items)}
    <p style="margin:16px 0 0;font-size:13px;color:{COLOR_MUTED};">
      Once dispatched, mark the order as <strong>"In Transit"</strong> from your dashboard.
    </p>
    {_btn("View order →", FRONTEND_URL + "/vendor/orders")}
    """
    send_email(
        vendor_email,
        f"🛍️ New sale — Order #{order_id[:8].upper()}",
        _base_html("New Sale", body),
    )


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_order_in_transit(
    email: str,
    name: str,
    order_id: str,
    delivery_address: str,
) -> None:
    """#7 — Shipping update for customers."""
    first = name.split()[0] if name else "there"
    body  = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      Your order is on the way! 🚚
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {first}, great news — your Farm-Connect order has been dispatched and
      is heading to you now.
    </p>
    {_info_table(
        _info_row("Order ID:", f"<code>#{order_id[:8].upper()}</code>"),
        _info_row("Delivering to:", delivery_address),
        _info_row("Status:", _badge("In Transit 🚚", COLOR_PRIMARY)),
    )}
    <p style="margin:16px 0 0;font-size:13px;color:{COLOR_MUTED};">
      Please ensure someone is available at the delivery address to receive your order.
    </p>
    {_btn("Track your order →", FRONTEND_URL + "/customer/orders/" + order_id)}
    """
    send_email(
        email,
        f"Your order #{order_id[:8].upper()} is on its way! 🚚",
        _base_html("Order In Transit", body),
    )


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_order_delivered(
    email: str,
    name: str,
    order_id: str,
) -> None:
    """#8 — Delivery confirmation for customers."""
    first = name.split()[0] if name else "there"
    body  = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      Order delivered! ✅
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {first}, your Farm-Connect order has been marked as delivered.
      We hope you enjoy your fresh produce!
    </p>
    {_info_table(
        _info_row("Order ID:", f"<code>#{order_id[:8].upper()}</code>"),
        _info_row("Status:", _badge("Delivered ✅", "#276749")),
    )}
    <p style="margin:16px 0 0;font-size:14px;color:{COLOR_TEXT};">
      If you have any issues with your delivery — wrong items, damaged goods,
      or missing products — please contact us within <strong>24 hours</strong>.
    </p>
    {_btn("View order & rate →", FRONTEND_URL + "/customer/orders/" + order_id)}
    """
    send_email(
        email,
        f"Your Farm-Connect order has been delivered ✅",
        _base_html("Order Delivered", body),
    )


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_order_cancelled(
    email: str,
    name: str,
    order_id: str,
    total_kobo: int,
    note: Optional[str] = None,
) -> None:
    """#9 — Order cancellation notice for customers."""
    first       = name.split()[0] if name else "there"
    note_block  = f"""
    {_divider()}
    <p style="margin:0;font-size:14px;color:{COLOR_TEXT};">
      <strong>Reason:</strong><br/>
      <span style="color:{COLOR_MUTED};">{note}</span>
    </p>""" if note else ""
    body = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_ERROR};font-size:22px;font-weight:800;">
      Order cancelled
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {first}, your order has been cancelled.
    </p>
    {_info_table(
        _info_row("Order ID:", f"<code>#{order_id[:8].upper()}</code>"),
        _info_row("Amount:", _naira(total_kobo)),
        _info_row("Refund:", "Credited back to your wallet"),
    )}
    {note_block}
    <p style="margin:16px 0 0;font-size:13px;color:{COLOR_MUTED};">
      The full amount of {_naira(total_kobo)} has been returned to your Farm-Connect wallet.
    </p>
    {_btn("Browse the market →", FRONTEND_URL + "/marketplace")}
    """
    send_email(
        email,
        f"Order #{order_id[:8].upper()} has been cancelled",
        _base_html("Order Cancelled", body),
    )


# ===========================================================================
# ③ WALLET NOTIFICATIONS
# ===========================================================================

@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_wallet_topup_receipt(
    email: str,
    name: str,
    amount_kobo: int,
    reference: str,
    new_balance_kobo: int,
) -> None:
    """#10 — Wallet top-up receipt for customers."""
    from datetime import datetime, timezone
    first = name.split()[0] if name else "there"
    date  = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    body  = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      Wallet funded successfully 💰
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {first}, your Farm-Connect wallet has been topped up.
    </p>
    {_info_table(
        _info_row("Amount added:", _naira(amount_kobo)),
        _info_row("New balance:", _naira(new_balance_kobo)),
        _info_row("Reference:", f"<code>{reference}</code>"),
        _info_row("Date:", date),
    )}
    <p style="margin:16px 0 0;font-size:13px;color:{COLOR_ERROR};">
      ⚠️ If you did not initiate this transaction, please contact 
      <a href="mailto:support@farmconnect.ng" style="color:{COLOR_PRIMARY};">
        support@farmconnect.ng
      </a> immediately.
    </p>
    {_btn("Shop now →", FRONTEND_URL + "/marketplace")}
    """
    send_email(
        email,
        f"Wallet funded — {_naira(amount_kobo)} added to your Farm-Connect wallet",
        _base_html("Wallet Top-Up", body),
    )


# ===========================================================================
# ④ PRODUCT NOTIFICATIONS
# ===========================================================================

@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_product_submitted(
    vendor_email: str,
    vendor_name: str,
    product_name: str,
    product_id: str,
) -> None:
    """#12 — Product listing submission confirmation for vendors."""
    body = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      Your listing is under review 🔍
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {vendor_name}, your product listing has been submitted and is being
      reviewed by our team. This usually takes under 24 hours.
    </p>
    {_info_table(
        _info_row("Product:", product_name),
        _info_row("Listing ID:", f"<code>{product_id[:8].upper()}</code>"),
        _info_row("Status:", _badge("Pending Approval", "#D97706")),
    )}
    <p style="margin:16px 0 0;font-size:13px;color:{COLOR_MUTED};">
      You'll receive an email as soon as the review is complete.
    </p>
    {_btn("View your listings →", FRONTEND_URL + "/vendor/products")}
    """
    send_email(
        vendor_email,
        f"Listing submitted — \"{product_name}\" is under review",
        _base_html("Listing Submitted", body),
    )


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_product_approved(
    vendor_email: str,
    vendor_name: str,
    product_name: str,
    product_id: str,
    price_kobo: int,
    stock: int,
) -> None:
    """#13 — Product listing approved by admin."""
    body = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      Your product is now live! ✅
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {vendor_name}, your product listing has been reviewed and approved.
      Customers can now find and purchase it on the marketplace.
    </p>
    {_info_table(
        _info_row("Product:", product_name),
        _info_row("Price:", _naira(price_kobo) + "/kg"),
        _info_row("Stock:", f"{stock} kg"),
        _info_row("Status:", _badge("Live ✅", "#276749")),
    )}
    {_btn("View your listing →", FRONTEND_URL + "/marketplace")}
    """
    send_email(
        vendor_email,
        f"✅ \"{product_name}\" is now live on Farm-Connect",
        _base_html("Product Approved", body),
    )


@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_product_rejected(
    vendor_email: str,
    vendor_name: str,
    product_name: str,
    product_id: str,
    reason: Optional[str] = None,
) -> None:
    """#14 — Product listing rejected by admin."""
    reason_block = f"""
    {_divider()}
    <p style="margin:0 0 8px;font-size:14px;font-weight:700;color:{COLOR_TEXT};">
      Reason for rejection:
    </p>
    <div style="background-color:#FFF5F5;border-left:4px solid {COLOR_ERROR};
                padding:12px 16px;border-radius:0 8px 8px 0;
                font-size:14px;color:{COLOR_TEXT};line-height:1.6;">
      {reason}
    </div>""" if reason else ""
    body = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_ERROR};font-size:22px;font-weight:800;">
      Your listing needs attention ⚠️
    </h2>
    <p style="margin:0 0 16px;color:{COLOR_MUTED};font-size:14px;">
      Hi {vendor_name}, unfortunately your product listing for 
      <strong>"{product_name}"</strong> was not approved at this time.
    </p>
    {_info_table(
        _info_row("Product:", product_name),
        _info_row("Status:", _badge("Rejected", COLOR_ERROR)),
    )}
    {reason_block}
    <p style="margin:20px 0 0;font-size:14px;color:{COLOR_TEXT};">
      You can edit and resubmit your listing from your vendor dashboard.
    </p>
    {_btn("Edit listing →", FRONTEND_URL + "/vendor/products")}
    """
    send_email(
        vendor_email,
        f"⚠️ Your listing \"{product_name}\" was not approved",
        _base_html("Listing Rejected", body),
    )


# ===========================================================================
# ⑤ WEEKLY DIGEST
# ===========================================================================

@celery_app.task(autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 5})
def send_weekly_vendor_digest(
    vendor_email: str,
    vendor_name: str,
    stats: dict,
) -> None:
    """
    #15 — Weekly sales digest for vendors.

    stats dict keys:
      week_label          str   e.g. "23–29 Jun 2025"
      revenue_kobo        int
      orders_count        int
      units_sold          int
      avg_rating          float
      top_product_name    str
      top_product_units   int
      low_stock_count     int
      low_stock_names     list[str]
    """
    from datetime import datetime, timezone
    week   = stats.get("week_label", "this week")
    rev    = stats.get("revenue_kobo", 0)
    orders = stats.get("orders_count", 0)
    units  = stats.get("units_sold", 0)
    rating = stats.get("avg_rating", 0.0)
    top_p  = stats.get("top_product_name", "—")
    top_u  = stats.get("top_product_units", 0)
    low_n  = stats.get("low_stock_count", 0)
    low_names = stats.get("low_stock_names", [])

    low_stock_block = ""
    if low_n > 0:
        names_str = ", ".join(low_names[:5])
        low_stock_block = f"""
        {_divider()}
        <p style="margin:0 0 8px;font-size:14px;font-weight:700;color:{COLOR_ERROR};">
          ⚠️ {low_n} product(s) running low on stock:
        </p>
        <p style="margin:0;font-size:13px;color:{COLOR_MUTED};">{names_str}</p>
        {_btn("Update stock →", FRONTEND_URL + "/vendor/products", COLOR_ERROR)}
        """

    body = f"""
    <h2 style="margin:0 0 4px;color:{COLOR_DARK};font-size:22px;font-weight:800;">
      📊 Your weekly summary — {week}
    </h2>
    <p style="margin:0 0 20px;color:{COLOR_MUTED};font-size:14px;">
      Hi {vendor_name}, here's how you did this week on Farm-Connect.
    </p>
    {_info_table(
        _info_row("💰 Revenue:", _naira(rev)),
        _info_row("📦 Orders fulfilled:", str(orders)),
        _info_row("🏋️ Units sold:", f"{units} kg"),
        _info_row("⭐ Avg. rating:", f"{rating:.1f} / 5.0"),
        _info_row("🏆 Top product:", f"{top_p} ({top_u} kg)"),
    )}
    {low_stock_block}
    {_btn("View full analytics →", FRONTEND_URL + "/vendor/analytics")}
    """
    send_email(
        vendor_email,
        f"📊 Your Farm-Connect summary — {week}",
        _base_html("Weekly Digest", body),
    )
