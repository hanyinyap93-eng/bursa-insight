"""Outbound email via SMTP (Gmail by default).

If SMTP credentials aren't configured, runs in DEV MODE: the message is logged
to the console instead of sent, so local development needs no email account.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..config import settings

log = logging.getLogger("bursa.mailer")


def is_configured() -> bool:
    return bool(settings.smtp_user and settings.smtp_password)


def send_email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send an email. Returns True if actually sent, False in dev mode.
    Raises on a real SMTP failure so the caller can surface it."""
    if not is_configured():
        log.warning("[DEV] SMTP not configured — would send to %s | %s\n%s",
                    to, subject, text)
        return False
    sender = settings.smtp_from or settings.smtp_user
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
        s.starttls(context=ctx)
        s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    log.info("sent '%s' to %s", subject, to)
    return True


def send_verification(to: str, link: str) -> bool:
    """Send (or dev-log) the account-confirmation email. Returns True if sent."""
    subject = "Confirm your Bursa Insight account"
    text = ("Welcome to Bursa Insight!\n\n"
            "Confirm your email address to finish creating your account:\n"
            f"{link}\n\n"
            "This link expires in 24 hours. If you didn't sign up, ignore this email.")
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:480px">
  <h2 style="margin:0 0 8px">Welcome to Bursa Insight</h2>
  <p style="color:#444">Confirm your email address to finish creating your account.</p>
  <p style="margin:22px 0">
    <a href="{link}" style="background:#3d7bff;color:#fff;text-decoration:none;
       padding:11px 20px;border-radius:8px;font-weight:600;display:inline-block">
       Confirm my email</a>
  </p>
  <p style="color:#888;font-size:12px">Or paste this link into your browser:<br>
    <a href="{link}">{link}</a></p>
  <p style="color:#888;font-size:12px">This link expires in 24 hours.
    If you didn't sign up, you can ignore this email.</p>
</div>"""
    return send_email(to, subject, text, html)
