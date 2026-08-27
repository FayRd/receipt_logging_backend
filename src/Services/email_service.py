"""
Email Service: Mailtrap SMTP Sandbox Dispatcher

Sends branded HTML + plain-text OTP emails (email verification and password reset) and cooldown notices via Mailtrap SMTP.
Mobile-first, Charcoal Slate Neumorphic styling without colour gradients, glows, or emojis.
Uses asyncio.to_thread for non-blocking SMTP dispatch.
"""

import asyncio
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from src.config import get_settings
from src.Infrastructure.logger import get_logger

logger = get_logger("Services.email_service")

_settings = get_settings()

# ── EMAIL VERIFICATION TEMPLATES ─────────────────────────────────────────────

_VERIFICATION_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Email Verification</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background-color: #121318; color: #E2E8F0; margin: 0; padding: 16px; }}
    .container {{ width: 100%; max-width: 440px; margin: 20px auto; background-color: #1E2028;
                  border: 1.5px solid #00E5A0; border-radius: 16px; overflow: hidden;
                  box-shadow: 4px 4px 12px #0a0b0e, -4px -4px 12px #222530; }}
    .header {{ background-color: #1E2028; padding: 28px 24px; text-align: center;
               border-bottom: 1px solid #282C38; }}
    .header h1 {{ color: #E2E8F0; margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
    .header p {{ color: #94A3B8; margin: 4px 0 0; font-size: 13px; }}
    .body {{ padding: 28px 24px; }}
    .greeting {{ font-size: 15px; color: #E2E8F0; font-weight: 600; margin: 0 0 14px; }}
    .message {{ font-size: 14px; color: #94A3B8; line-height: 1.6; margin: 0 0 24px; }}
    .otp-box {{ background-color: #161820; border: 1px solid #282C38; border-radius: 12px;
                text-align: center; padding: 20px 16px; margin: 0 0 24px;
                box-shadow: inset 2px 2px 5px #0f1015, inset -2px -2px 5px #242733; }}
    .otp-box .otp-label {{ font-size: 11px; color: #94A3B8; text-transform: uppercase;
                            letter-spacing: 1.5px; margin-bottom: 8px; font-weight: 600; }}
    .otp-box .otp-code {{ font-size: 36px; font-weight: 800; color: #00E5A0;
                           letter-spacing: 8px; margin: 0; }}
    .notice {{ background-color: #161820; border-left: 3px solid #00E5A0; border-radius: 6px;
               padding: 12px 16px; margin-bottom: 24px;
               box-shadow: inset 1px 1px 3px #0f1015, inset -1px -1px 3px #242733; }}
    .notice p {{ font-size: 13px; color: #94A3B8; margin: 0; line-height: 1.5; }}
    .notice strong {{ color: #E2E8F0; }}
    .footer {{ padding: 18px 24px; border-top: 1px solid #282C38; text-align: center; }}
    .footer p {{ font-size: 12px; color: #64748B; margin: 0; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>SancFund</h1>
      <p>Receipt & Expense Manager</p>
    </div>
    <div class="body">
      <p class="greeting">Hello, {username}!</p>
      <p class="message">
        You requested to verify your email address. Use the verification code below to
        complete the process. This code is valid for <strong style="color: #E2E8F0;">10 minutes</strong>.
      </p>
      <div class="otp-box">
        <div class="otp-label">Verification Code</div>
        <div class="otp-code">{otp}</div>
      </div>
      <div class="notice">
        <p><strong>Security Notice:</strong> Never share this code with anyone.
           SancFund will never ask for your verification code by phone or email.</p>
      </div>
      <p class="message" style="margin-bottom: 0;">If you did not request this verification, you can safely ignore this email.</p>
    </div>
    <div class="footer">
      <p>© 2026 SancFund · {from_address}</p>
      <p style="margin-top: 4px;">This is an automated message — please do not reply.</p>
    </div>
  </div>
</body>
</html>
"""

_VERIFICATION_TEXT_TEMPLATE = """\
SancFund — Email Verification

Hello, {username}!

Your verification code is: {otp}

This code is valid for 10 minutes. Do not share it with anyone.

Security Notice: Never share this code with anyone. SancFund will never ask for your verification code by phone or email.

If you did not request this verification, you can safely ignore this email.

— SancFund Team
"""

# ── PASSWORD RESET TEMPLATES ─────────────────────────────────────────────────

_PASSWORD_RESET_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Password Reset</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background-color: #121318; color: #E2E8F0; margin: 0; padding: 16px; }}
    .container {{ width: 100%; max-width: 440px; margin: 20px auto; background-color: #1E2028;
                  border: 1.5px solid #00E5A0; border-radius: 16px; overflow: hidden;
                  box-shadow: 4px 4px 12px #0a0b0e, -4px -4px 12px #222530; }}
    .header {{ background-color: #1E2028; padding: 28px 24px; text-align: center;
               border-bottom: 1px solid #282C38; }}
    .header h1 {{ color: #E2E8F0; margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
    .header p {{ color: #94A3B8; margin: 4px 0 0; font-size: 13px; }}
    .body {{ padding: 28px 24px; }}
    .greeting {{ font-size: 15px; color: #E2E8F0; font-weight: 600; margin: 0 0 14px; }}
    .message {{ font-size: 14px; color: #94A3B8; line-height: 1.6; margin: 0 0 24px; }}
    .otp-box {{ background-color: #161820; border: 1px solid #282C38; border-radius: 12px;
                text-align: center; padding: 20px 16px; margin: 0 0 24px;
                box-shadow: inset 2px 2px 5px #0f1015, inset -2px -2px 5px #242733; }}
    .otp-box .otp-label {{ font-size: 11px; color: #94A3B8; text-transform: uppercase;
                            letter-spacing: 1.5px; margin-bottom: 8px; font-weight: 600; }}
    .otp-box .otp-code {{ font-size: 36px; font-weight: 800; color: #00E5A0;
                           letter-spacing: 8px; margin: 0; }}
    .notice {{ background-color: #161820; border-left: 3px solid #00E5A0; border-radius: 6px;
               padding: 12px 16px; margin-bottom: 24px;
               box-shadow: inset 1px 1px 3px #0f1015, inset -1px -1px 3px #242733; }}
    .notice p {{ font-size: 13px; color: #94A3B8; margin: 0; line-height: 1.5; }}
    .notice strong {{ color: #E2E8F0; }}
    .footer {{ padding: 18px 24px; border-top: 1px solid #282C38; text-align: center; }}
    .footer p {{ font-size: 12px; color: #64748B; margin: 0; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>SancFund</h1>
      <p>Password Reset Request</p>
    </div>
    <div class="body">
      <p class="greeting">Hello, {username}!</p>
      <p class="message">
        We received a request to reset your password. Use the reset code below to
        proceed with creating a new password. This code is valid for <strong style="color: #E2E8F0;">15 minutes</strong>.
      </p>
      <div class="otp-box">
        <div class="otp-label">Password Reset Code</div>
        <div class="otp-code">{otp}</div>
      </div>
      <div class="notice">
        <p><strong>Security Notice:</strong> If you did not request a password reset,
           please ignore this email or change your password immediately if you suspect unauthorized activity.</p>
      </div>
    </div>
    <div class="footer">
      <p>© 2026 SancFund · {from_address}</p>
      <p style="margin-top: 4px;">This is an automated message — please do not reply.</p>
    </div>
  </div>
</body>
</html>
"""

_PASSWORD_RESET_TEXT_TEMPLATE = """\
SancFund — Password Reset Request

Hello, {username}!

Your password reset code is: {otp}

This code is valid for 15 minutes. Do not share it with anyone.

Security Notice: If you did not request a password reset, please ignore this email or change your password immediately if you suspect unauthorized activity.

— SancFund Team
"""

# ── PASSWORD RESET COOLDOWN ADVISORY TEMPLATES ───────────────────────────────

_PASSWORD_RESET_COOLDOWN_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Password Reset Notice</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background-color: #121318; color: #E2E8F0; margin: 0; padding: 16px; }}
    .container {{ width: 100%; max-width: 440px; margin: 20px auto; background-color: #1E2028;
                  border: 1.5px solid #00E5A0; border-radius: 16px; overflow: hidden;
                  box-shadow: 4px 4px 12px #0a0b0e, -4px -4px 12px #222530; }}
    .header {{ background-color: #1E2028; padding: 28px 24px; text-align: center;
               border-bottom: 1px solid #282C38; }}
    .header h1 {{ color: #E2E8F0; margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
    .header p {{ color: #94A3B8; margin: 4px 0 0; font-size: 13px; }}
    .body {{ padding: 28px 24px; }}
    .greeting {{ font-size: 15px; color: #E2E8F0; font-weight: 600; margin: 0 0 14px; }}
    .message {{ font-size: 14px; color: #94A3B8; line-height: 1.6; margin: 0 0 24px; }}
    .cooldown-box {{ background-color: #161820; border: 1px solid #282C38; border-radius: 12px;
                    text-align: center; padding: 20px 16px; margin: 0 0 24px;
                    box-shadow: inset 2px 2px 5px #0f1015, inset -2px -2px 5px #242733; }}
    .cooldown-box .cooldown-label {{ font-size: 11px; color: #94A3B8; text-transform: uppercase;
                                    letter-spacing: 1.5px; margin-bottom: 8px; font-weight: 600; }}
    .cooldown-box .cooldown-time {{ font-size: 22px; font-weight: 800; color: #00E5A0;
                                   letter-spacing: 0.5px; margin: 0; }}
    .notice {{ background-color: #161820; border-left: 3px solid #00E5A0; border-radius: 6px;
               padding: 12px 16px; margin-bottom: 24px;
               box-shadow: inset 1px 1px 3px #0f1015, inset -1px -1px 3px #242733; }}
    .notice p {{ font-size: 13px; color: #94A3B8; margin: 0; line-height: 1.5; }}
    .notice strong {{ color: #E2E8F0; }}
    .footer {{ padding: 18px 24px; border-top: 1px solid #282C38; text-align: center; }}
    .footer p {{ font-size: 12px; color: #64748B; margin: 0; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>SancFund</h1>
      <p>Security & Cooldown Notice</p>
    </div>
    <div class="body">
      <p class="greeting">Hello, {username}!</p>
      <p class="message">
        We received a request to reset your password. However, your account is currently in a
        <strong style="color: #E2E8F0;">7-day security cooldown</strong> period following a recent password change.
      </p>
      <div class="cooldown-box">
        <div class="cooldown-label">Password Change Allowed In</div>
        <div class="cooldown-time">{countdown_str}</div>
      </div>
      <div class="notice">
        <p><strong>Security Policy:</strong> Passwords can only be changed once every 7 days
           to safeguard your account. If you did not request this password reset, no action is needed
           and your account remains secure.</p>
      </div>
    </div>
    <div class="footer">
      <p>© 2026 SancFund · {from_address}</p>
      <p style="margin-top: 4px;">This is an automated message — please do not reply.</p>
    </div>
  </div>
</body>
</html>
"""

_PASSWORD_RESET_COOLDOWN_TEXT_TEMPLATE = """\
SancFund — Password Reset Cooldown Notice

Hello, {username}!

We received a request to reset your password. However, your account is currently in a 7-day security cooldown period following a recent password change.

For your account protection, passwords can only be changed once every 7 days.

Password change allowed in:
{countdown_str}

Security Policy: Passwords can only be changed once every 7 days to safeguard your account. If you did not request this password reset, no action is needed and your account remains secure.

— SancFund Team
"""


# ── MESSAGE BUILDERS ─────────────────────────────────────────────────────────

def _build_verification_message(to_email: str, otp: str, username: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your SancFund Verification Code: {otp}"
    msg["From"] = f"{_settings.mail_from_name} <{_settings.mail_from_address}>"
    msg["To"] = to_email

    text_part = MIMEText(
        _VERIFICATION_TEXT_TEMPLATE.format(
            username=username, otp=otp, from_address=_settings.mail_from_address
        ),
        "plain",
        "utf-8",
    )
    html_part = MIMEText(
        _VERIFICATION_HTML_TEMPLATE.format(
            username=username, otp=otp, from_address=_settings.mail_from_address
        ),
        "html",
        "utf-8",
    )
    msg.attach(text_part)
    msg.attach(html_part)
    return msg


def _build_password_reset_message(to_email: str, otp: str, username: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your SancFund Password Reset Code: {otp}"
    msg["From"] = f"{_settings.mail_from_name} <{_settings.mail_from_address}>"
    msg["To"] = to_email

    text_part = MIMEText(
        _PASSWORD_RESET_TEXT_TEMPLATE.format(
            username=username, otp=otp, from_address=_settings.mail_from_address
        ),
        "plain",
        "utf-8",
    )
    html_part = MIMEText(
        _PASSWORD_RESET_HTML_TEMPLATE.format(
            username=username, otp=otp, from_address=_settings.mail_from_address
        ),
        "html",
        "utf-8",
    )
    msg.attach(text_part)
    msg.attach(html_part)
    return msg


def _build_password_reset_cooldown_message(to_email: str, countdown_str: str, username: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "SancFund — Password Reset Request Notice (Cooldown Active)"
    msg["From"] = f"{_settings.mail_from_name} <{_settings.mail_from_address}>"
    msg["To"] = to_email

    text_part = MIMEText(
        _PASSWORD_RESET_COOLDOWN_TEXT_TEMPLATE.format(
            username=username, countdown_str=countdown_str, from_address=_settings.mail_from_address
        ),
        "plain",
        "utf-8",
    )
    html_part = MIMEText(
        _PASSWORD_RESET_COOLDOWN_HTML_TEMPLATE.format(
            username=username, countdown_str=countdown_str, from_address=_settings.mail_from_address
        ),
        "html",
        "utf-8",
    )
    msg.attach(text_part)
    msg.attach(html_part)
    return msg


# ── SMTP SENDER ──────────────────────────────────────────────────────────────

def _send_smtp_sync(msg: MIMEMultipart, to_email: str) -> bool:
    """Synchronous SMTP delivery — runs in a thread via asyncio.to_thread."""
    last_error: Exception | None = None
    for attempt in range(2):  # 1 retry
        try:
            with smtplib.SMTP(_settings.mail_host, _settings.mail_port, timeout=5) as server:
                server.starttls()
                server.login(_settings.mail_username, _settings.mail_password)
                server.sendmail(_settings.mail_from_address, [to_email], msg.as_string())
            logger.info(
                "Email dispatched to %s via %s:%s (attempt %d)",
                to_email, _settings.mail_host, _settings.mail_port, attempt + 1,
            )
            return True
        except Exception as e:
            last_error = e
            if attempt == 0:
                logger.warning("SMTP attempt 1 failed for %s: %s — retrying...", to_email, e)
                time.sleep(0.5)

    logger.error("All SMTP delivery attempts failed for %s: %s", to_email, last_error)
    return False


# ── PUBLIC API ───────────────────────────────────────────────────────────────

async def send_verification_email(to_email: str, otp: str, username: str = "User") -> bool:
    """Dispatch an email verification OTP to the given address via Mailtrap SMTP.

    Returns True if delivery succeeded, False if SMTP credentials are missing or dispatch fails.
    """
    if not _settings.mail_username or not _settings.mail_password:
        logger.warning("Mailtrap SMTP credentials not configured — skipping email dispatch to %s", to_email)
        return False

    try:
        msg = _build_verification_message(to_email, otp, username)
        return await asyncio.to_thread(_send_smtp_sync, msg, to_email)
    except Exception as e:
        logger.error("Unexpected error during verification email dispatch to %s: %s", to_email, e, exc_info=True)
        return False


async def send_password_reset_email(to_email: str, otp: str, username: str = "User") -> bool:
    """Dispatch a password reset OTP to the given address via Mailtrap SMTP.

    Returns True if delivery succeeded, False if SMTP credentials are missing or dispatch fails.
    """
    if not _settings.mail_username or not _settings.mail_password:
        logger.warning("Mailtrap SMTP credentials not configured — skipping password reset email dispatch to %s", to_email)
        return False

    try:
        msg = _build_password_reset_message(to_email, otp, username)
        return await asyncio.to_thread(_send_smtp_sync, msg, to_email)
    except Exception as e:
        logger.error("Unexpected error during password reset email dispatch to %s: %s", to_email, e, exc_info=True)
        return False


async def send_password_reset_cooldown_email(to_email: str, countdown_str: str, username: str = "User") -> bool:
    """Dispatch a password reset cooldown advisory email via Mailtrap SMTP.

    Stating the remaining day(s) and hour(s) before a password change can be requested.
    Returns True if delivery succeeded, False if SMTP credentials are missing or dispatch fails.
    """
    if not _settings.mail_username or not _settings.mail_password:
        logger.warning("Mailtrap SMTP credentials not configured — skipping cooldown email dispatch to %s", to_email)
        return False

    try:
        msg = _build_password_reset_cooldown_message(to_email, countdown_str, username)
        return await asyncio.to_thread(_send_smtp_sync, msg, to_email)
    except Exception as e:
        logger.error("Unexpected error during cooldown email dispatch to %s: %s", to_email, e, exc_info=True)
        return False
