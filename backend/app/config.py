"""App configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

# Absolute path to backend/.env so it is found no matter what working directory
# the server is launched from (e.g. project root with --app-dir, vs backend/).
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    app_name: str = "Bursa Insight API"
    version: str = "0.1.0"
    # comma-separated origins for CORS; "*" in dev
    cors_origins: str = "*"
    default_lookback: str = "1y"
    # guest vs authed: which indices a guest may view in full
    guest_indices: str = "KLCI"

    # --- auth (gates GEX / Market Index / R-Appetite) ---
    # Google OAuth Web Client ID; incoming Google ID tokens must carry this as
    # their audience. Must match the frontend <meta name="google-client-id">.
    google_client_id: str = (
        "1005786557822-q5i4b4eqbkslkolnro3stbesa4kjg368.apps.googleusercontent.com"
    )
    # HS256 signing secret for the session JWTs we mint after verifying Google.
    # OVERRIDE IN PRODUCTION via BURSA_JWT_SECRET — the dev default is public.
    jwt_secret: str = "dev-insecure-change-me-via-BURSA_JWT_SECRET"
    jwt_ttl_hours: int = 24 * 7   # session lifetime
    verify_ttl_hours: int = 24    # email-confirmation link lifetime
    # Comma-separated allowlist of emails permitted to view the admin login log
    # (GET /api/admin/logins). Set BURSA_ADMIN_EMAILS in prod. Defaults to the
    # owner's Gmail so the endpoint works out of the box for you.
    admin_emails: str = "hanyinyap93@gmail.com"
    # Master switch for email+password sign-in/sign-up. Off = Google-only (no
    # email sending needed at all). Turn on (BURSA_EMAIL_AUTH_ENABLED=true) once
    # a real sending domain is verified so confirmation emails deliver reliably.
    email_auth_enabled: bool = False

    # --- outbound email (email-confirmation links) ---
    # Gmail: host smtp.gmail.com, port 587, user = your gmail, password = a
    # 16-char Google "app password" (NOT your normal password). If smtp_user /
    # smtp_password are empty the app runs in DEV MODE: it logs the confirmation
    # link to the console instead of sending mail (so local testing still works).
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""           # defaults to smtp_user if empty
    # Public base URL used to build confirmation links, e.g.
    # https://bursa-insight.onrender.com. If empty, derived from the request.
    public_url: str = ""
    # Comma-separated allowlist of email domains permitted for email+password
    # sign-up. Empty = allow any. Default limits to Gmail while sending from a
    # personal Gmail (reliable Gmail→Gmail delivery) before a domain is verified.
    # Google sign-in is unaffected (any Google account works).
    signup_email_domains: str = "gmail.com,googlemail.com"

    class Config:
        env_prefix = "BURSA_"
        env_file = str(_ENV_FILE)


settings = Settings()
