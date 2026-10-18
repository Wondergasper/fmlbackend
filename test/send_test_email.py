"""
Quick email delivery test.
Run this on your machine (not in sandbox) to verify email works.

Usage:
  python send_test_email.py you@example.com
"""

import sys, os, json, urllib.request

# Load .env
from dotenv import load_dotenv
load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "noreply@farmconnect.ng")
FROM_NAME      = os.getenv("FROM_NAME", "Farm-Connect")

to = sys.argv[1] if len(sys.argv) > 1 else "gasperwonder@gmail.com"

# ── Try Resend ──
if RESEND_API_KEY:
    print(f"  Trying Resend...  to={to}")
    payload = json.dumps({
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": [to],
        "subject": "Farm-Connect Test — OTP: 1234",
        "html": f"<h1>Test</h1><p>Your OTP code is: <strong>1234</strong></p>",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "Farm-Connect/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  [OK] Resend: HTTP {resp.status} {resp.read().decode()}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  [FAIL] Resend: HTTP {e.code} — {body}")
        if "domain" in body.lower():
            print("  → Domain 'farmconnect.ng' not verified at Resend.")
            print("  → Go to https://resend.com/domains and add DNS TXT record.")
    except Exception as e:
        print(f"  [FAIL] Resend: {e}")

# ── Try SendGrid ──
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
if SENDGRID_API_KEY and "replace" not in SENDGRID_API_KEY.lower():
    print(f"  Trying SendGrid...  to={to}")
    payload = json.dumps({
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": FROM_EMAIL, "name": FROM_NAME},
        "subject": "Farm-Connect Test — OTP: 1234",
        "content": [{"type": "text/html", "value": "<h1>Test</h1><p>OTP: <strong>1234</strong></p>"}],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=payload,
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  [OK] SendGrid: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"  [FAIL] SendGrid: HTTP {e.code} — {e.read().decode()}")
    except Exception as e:
        print(f"  [FAIL] SendGrid: {e}")
else:
    print(f"  [SKIP] SendGrid: not configured (SENDGRID_API_KEY is placeholder or empty)")

# ── Try SMTP ──
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
if SMTP_USER and "your" not in SMTP_USER.lower():
    print(f"  Trying SMTP...  to={to}")
    import smtplib
    from email.mime.text import MIMEText
    try:
        msg = MIMEText("<h1>Test</h1><p>OTP: <strong>1234</strong></p>", "html")
        msg["Subject"] = "Farm-Connect Test"
        msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"] = to
        with smtplib.SMTP(os.getenv("SMTP_HOST","smtp.gmail.com"), 587, timeout=10) as server:
            server.ehlo(); server.starttls(); server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, to, msg.as_string())
        print(f"  [OK] SMTP: sent")
    except Exception as e:
        print(f"  [FAIL] SMTP: {e}")
else:
    print(f"  [SKIP] SMTP: not configured")

print("\nDone. Check your inbox at", to)

