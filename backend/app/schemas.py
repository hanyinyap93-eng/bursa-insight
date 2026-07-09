"""Pydantic request/response models."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ScreenRequest(BaseModel):
    index: str = "KLCI"
    lookback: str = "1y"
    corr_window: Optional[int] = None
    above_sma: Optional[bool] = None
    momentum_up: Optional[bool] = None
    rsi_overbought: Optional[bool] = None
    rsi_oversold: Optional[bool] = None
    new_high: Optional[bool] = None
    new_low: Optional[bool] = None
    min_correlation: Optional[float] = None
    min_return_pct: Optional[float] = None
    max_return_pct: Optional[float] = None
    sectors: Optional[list[str]] = None
    healthy_only: Optional[bool] = None


class AlertRequest(BaseModel):
    metric: str = Field(..., examples=["index_health", "sector_health:TECHNOLOGY"])
    op: str = Field(..., examples=["below", "cross_above"])
    threshold: float
    label: Optional[str] = None


class BacktestHealthRequest(BaseModel):
    index: str = "KLCI"
    lookback: str = "1y"
    entry: float = 0.0
    exit_: float = Field(-10.0, alias="exit")
    cost_bps: float = 0.0

    class Config:
        populate_by_name = True


class BacktestScreenRequest(BaseModel):
    index: str = "KLCI"
    lookback: str = "1y"
    require_above_sma: bool = True
    require_momentum_up: bool = True
    cost_bps: float = 0.0


class GoogleAuthRequest(BaseModel):
    """The Google ID token (JWT) returned by Google Identity Services on the
    client, to be verified server-side before a session token is issued."""
    credential: str


class EmailSignupRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class EmailLoginRequest(BaseModel):
    email: str
    password: str


class VerifyRequest(BaseModel):
    token: str


class ResendRequest(BaseModel):
    email: str


class PortfolioAddRequest(BaseModel):
    code: str = ""
    ticker: str
    name: Optional[str] = None
    shares: float
    buy_date: str   # ISO "YYYY-MM-DD"
