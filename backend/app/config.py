"""App configuration."""
from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Bursa Insight API"
    version: str = "0.1.0"
    # comma-separated origins for CORS; "*" in dev
    cors_origins: str = "*"
    default_lookback: str = "1y"
    # guest vs authed: which indices a guest may view in full
    guest_indices: str = "KLCI"

    class Config:
        env_prefix = "BURSA_"
        env_file = ".env"


settings = Settings()
