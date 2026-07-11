"""Per-user stock portfolios (shares + buy date) with buy-and-hold past
performance. Holdings persist per user in the same DB as accounts; performance
is computed from the klsescreener price feed and benchmarked against the KLCI.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import Float, Integer, String, create_engine  # noqa: F401 (create_engine re-export unused)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from . import breadth as breadth_mod
from . import klse_prices
from .users import _engine, _norm

_LB_STEPS = [("1mo", 31), ("3mo", 93), ("6mo", 186), ("1y", 400),
             ("2y", 760), ("5y", 1850), ("10y", 3700), ("max", 5000)]


class Base(DeclarativeBase):
    pass


class Holding(Base):
    __tablename__ = "portfolio_holdings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String, index=True)   # owner email (normalised)
    code: Mapped[str] = mapped_column(String)               # e.g. "1155"
    ticker: Mapped[str] = mapped_column(String)             # e.g. "1155.KL"
    name: Mapped[str] = mapped_column(String, default="")
    shares: Mapped[float] = mapped_column(Float)
    buy_date: Mapped[str] = mapped_column(String)           # ISO "YYYY-MM-DD"


Base.metadata.create_all(_engine)


def _to_dict(h: Holding) -> dict:
    return {"id": h.id, "code": h.code, "ticker": h.ticker, "name": h.name,
            "shares": h.shares, "buy_date": h.buy_date}


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def list_holdings(user: str) -> list[dict]:
    with Session(_engine) as s:
        rows = s.query(Holding).filter_by(user=_norm(user)).order_by(Holding.id).all()
        return [_to_dict(h) for h in rows]


def add_holding(user: str, code: str, ticker: str, name: str,
                shares: float, buy_date: str) -> dict:
    try:
        shares = float(shares)
    except (TypeError, ValueError):
        raise ValueError("shares must be a number")
    if shares <= 0:
        raise ValueError("shares must be greater than 0")
    try:
        bd = date.fromisoformat((buy_date or "").strip())
    except ValueError:
        raise ValueError("buy date must be YYYY-MM-DD")
    if bd > date.today():
        raise ValueError("buy date can't be in the future")
    if not (ticker or "").strip():
        raise ValueError("a stock is required")
    with Session(_engine) as s:
        h = Holding(user=_norm(user), code=str(code or "").strip(),
                    ticker=str(ticker).strip(), name=str(name or "").strip(),
                    shares=shares, buy_date=bd.isoformat())
        s.add(h)
        s.commit()
        return _to_dict(h)


def remove_holding(user: str, hid: int) -> bool:
    with Session(_engine) as s:
        h = s.get(Holding, hid)
        if h is None or h.user != _norm(user):
            return False
        s.delete(h)
        s.commit()
        return True


# --------------------------------------------------------------------------- #
# Performance (buy-and-hold)
# --------------------------------------------------------------------------- #
def _lookback_for(earliest_iso: str) -> str:
    days = (date.today() - date.fromisoformat(earliest_iso)).days
    for key, dd in _LB_STEPS:
        if dd >= days + 10:
            return key
    return "max"


def _empty(as_of=None):
    return {"holdings": [], "dates": [], "value": [], "benchmark": [],
            "totals": {"cost": 0, "value": 0, "gain": 0, "gain_pct": 0, "as_of": as_of}}


def _rebase100(closes, index) -> list:
    """Align a daily-close series to `index` and rebase it to 100 at the first
    date, so different indices/portfolios are directly comparable. [] if empty."""
    s = closes.reindex(index).ffill().bfill()
    if s.isna().all():
        return []
    base = float(s.iloc[0])
    if not base:
        return []
    return [round(100.0 * float(x) / base, 2) for x in s.values]


def performance(user: str) -> dict:
    holds = list_holdings(user)
    if not holds:
        return _empty()

    lb = _lookback_for(min(h["buy_date"] for h in holds))
    tickers = list({h["ticker"] for h in holds})
    panel = klse_prices.close_panel(tickers, lb)
    panel.index = pd.to_datetime(panel.index).normalize()
    panel = panel.sort_index().ffill()

    total = pd.Series(0.0, index=panel.index)
    per: list[dict] = []
    for h in holds:
        col = panel.get(h["ticker"])
        row = {**h}
        if col is None or col.dropna().empty:
            per.append({**row, "error": "no price data"})
            continue
        bd = pd.Timestamp(h["buy_date"])
        after = col[col.index >= bd].dropna()
        if after.empty:
            per.append({**row, "error": "no prices since buy date"})
            continue
        buy_price = float(after.iloc[0])
        last_price = float(col.dropna().iloc[-1])
        cost = h["shares"] * buy_price
        value = h["shares"] * last_price
        # this holding contributes shares*price to the portfolio from buy date on
        contrib = (col.where(col.index >= bd) * h["shares"]).fillna(0.0)
        total = total.add(contrib, fill_value=0.0)
        per.append({**row, "buy_price": round(buy_price, 4), "last_price": round(last_price, 4),
                    "cost": round(cost, 2), "value": round(value, 2),
                    "gain": round(value - cost, 2),
                    "gain_pct": round((value / cost - 1) * 100, 2) if cost else 0.0})

    # trim leading dates before the first holding was bought (value == 0)
    nz = total[total > 0]
    if not nz.empty:
        total = total.loc[nz.index[0]:]
    dates = [str(d.date()) for d in total.index]
    value = [round(float(x), 2) for x in total.values]

    # KLCI benchmark, rebased to the portfolio's starting value
    benchmark: list[float] = []
    try:
        kl = breadth_mod.index_ohlc("KLCI", lb)
        kl_s = pd.Series(kl["close"], index=pd.to_datetime(kl["dates"]).normalize())
        kl_s = kl_s.reindex(total.index).ffill().bfill()
        if value and not kl_s.isna().all():
            base_v, base_k = value[0], float(kl_s.iloc[0])
            if base_k:
                benchmark = [round(base_v * float(x) / base_k, 2) for x in kl_s.values]
    except Exception:  # noqa: BLE001 - benchmark is best-effort
        benchmark = []

    # Rebased-to-100 relative performance: the portfolio and the benchmark
    # indices (FBM KLCI / MID 70 / ACE) all indexed to 100 at the portfolio's
    # earliest buy-in date, so their growth is directly comparable.
    rebased = {"dates": dates, "portfolio": [], "indices": {}}
    if value and value[0]:
        rebased["portfolio"] = [round(100.0 * v / value[0], 2) for v in value]
        idx_series = {}
        try:  # KLCI via the reliable ^KLSE OHLC feed
            kl = breadth_mod.index_ohlc("KLCI", lb)
            idx_series["FBM KLCI"] = pd.Series(
                kl["close"], index=pd.to_datetime(kl["dates"]).normalize())
        except Exception:  # noqa: BLE001 - benchmark is best-effort
            pass
        for label, code in (("FBM MID 70", "0863I"), ("FBM ACE", "0871I")):
            try:
                s = klse_prices.history(code, lookback=lb)["Close"].dropna()
                s.index = pd.to_datetime(s.index).normalize()
                idx_series[label] = s
            except Exception:  # noqa: BLE001
                pass
        for label, s in idx_series.items():
            r = _rebase100(s, total.index)
            if r:
                rebased["indices"][label] = r

    priced = [p for p in per if "cost" in p]
    cost_tot = round(sum(p["cost"] for p in priced), 2)
    value_tot = round(sum(p["value"] for p in priced), 2)
    return {
        "holdings": per,
        "dates": dates,
        "value": value,
        "benchmark": benchmark,
        "rebased": rebased,
        "totals": {
            "cost": cost_tot, "value": value_tot,
            "gain": round(value_tot - cost_tot, 2),
            "gain_pct": round((value_tot / cost_tot - 1) * 100, 2) if cost_tot else 0.0,
            "as_of": dates[-1] if dates else None,
        },
    }
