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
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True, nullable=True)  # -> portfolios.id
    code: Mapped[str] = mapped_column(String)               # e.g. "1155"
    ticker: Mapped[str] = mapped_column(String)             # e.g. "1155.KL"
    name: Mapped[str] = mapped_column(String, default="")
    shares: Mapped[float] = mapped_column(Float)
    buy_date: Mapped[str] = mapped_column(String)           # ISO "YYYY-MM-DD"


class Portfolio(Base):
    __tablename__ = "portfolios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String, index=True)   # owner email (normalised)
    name: Mapped[str] = mapped_column(String, default="My portfolio")
    position: Mapped[int] = mapped_column(Integer, default=0)   # display order 0..2


MAX_PORTFOLIOS = 3

Base.metadata.create_all(_engine)


def _ensure_pf_schema() -> None:
    """Add portfolio_holdings.portfolio_id to a pre-existing table (create_all
    won't ALTER). Old single-portfolio rows keep NULL until adopted by the
    user's default portfolio on first list_portfolios()."""
    from sqlalchemy import inspect, text
    cols = [c["name"] for c in inspect(_engine).get_columns("portfolio_holdings")]
    if "portfolio_id" not in cols:
        with _engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE portfolio_holdings ADD COLUMN portfolio_id INTEGER"))


_ensure_pf_schema()


def _to_dict(h: Holding) -> dict:
    return {"id": h.id, "code": h.code, "ticker": h.ticker, "name": h.name,
            "shares": h.shares, "buy_date": h.buy_date}


# --------------------------------------------------------------------------- #
# Portfolios (up to MAX_PORTFOLIOS per user, renameable)
# --------------------------------------------------------------------------- #
def _default_name(pos: int) -> str:
    return "My portfolio" if pos == 0 else f"Portfolio {pos + 1}"


def list_portfolios(user: str) -> list[dict]:
    """User's portfolios (auto-bootstraps a default one on first use, adopting
    any legacy single-portfolio holdings that predate multi-portfolio support)."""
    u = _norm(user)
    with Session(_engine) as s:
        rows = s.query(Portfolio).filter_by(user=u).order_by(
            Portfolio.position, Portfolio.id).all()
        if not rows:
            p = Portfolio(user=u, name="My portfolio", position=0)
            s.add(p); s.commit(); s.refresh(p)
            s.query(Holding).filter(Holding.user == u,
                                    Holding.portfolio_id.is_(None)).update(
                {Holding.portfolio_id: p.id}); s.commit()
            rows = [p]
        return [{"id": r.id, "name": r.name, "position": r.position} for r in rows]


def create_portfolio(user: str, name: str = "") -> dict:
    u = _norm(user)
    with Session(_engine) as s:
        existing = s.query(Portfolio).filter_by(user=u).order_by(Portfolio.position).all()
        if len(existing) >= MAX_PORTFOLIOS:
            raise ValueError(f"you can have at most {MAX_PORTFOLIOS} portfolios")
        pos = max((p.position for p in existing), default=-1) + 1
        nm = (name or "").strip()[:40] or _default_name(pos)
        p = Portfolio(user=u, name=nm, position=pos)
        s.add(p); s.commit(); s.refresh(p)
        return {"id": p.id, "name": p.name, "position": p.position}


def rename_portfolio(user: str, pid: int, name: str) -> dict:
    nm = (name or "").strip()[:40]
    if not nm:
        raise ValueError("name is required")
    with Session(_engine) as s:
        p = s.get(Portfolio, pid)
        if p is None or p.user != _norm(user):
            raise ValueError("portfolio not found")
        p.name = nm; s.commit()
        return {"id": p.id, "name": p.name, "position": p.position}


def delete_portfolio(user: str, pid: int) -> bool:
    u = _norm(user)
    with Session(_engine) as s:
        p = s.get(Portfolio, pid)
        if p is None or p.user != u:
            return False
        s.query(Holding).filter_by(user=u, portfolio_id=pid).delete()
        s.delete(p); s.commit()
        return True


def _resolve_pid(user: str, pid: int | None) -> int:
    """Return a valid portfolio id owned by the user; None -> their default."""
    u = _norm(user)
    if pid is not None:
        with Session(_engine) as s:
            p = s.get(Portfolio, pid)
            if p is None or p.user != u:
                raise ValueError("portfolio not found")
            return pid
    return list_portfolios(user)[0]["id"]   # bootstraps a default if needed


# --------------------------------------------------------------------------- #
# Holdings CRUD (scoped to a portfolio)
# --------------------------------------------------------------------------- #
def list_holdings(user: str, pid: int | None = None) -> list[dict]:
    u = _norm(user)
    with Session(_engine) as s:
        q = s.query(Holding).filter_by(user=u)
        if pid is not None:
            q = q.filter_by(portfolio_id=pid)
        return [_to_dict(h) for h in q.order_by(Holding.id).all()]


def add_holding(user: str, code: str, ticker: str, name: str,
                shares: float, buy_date: str, pid: int | None = None) -> dict:
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
    pid = _resolve_pid(user, pid)
    with Session(_engine) as s:
        h = Holding(user=_norm(user), portfolio_id=pid, code=str(code or "").strip(),
                    ticker=str(ticker).strip(), name=str(name or "").strip(),
                    shares=shares, buy_date=bd.isoformat())
        s.add(h)
        s.commit()
        return _to_dict(h)


def remove_holding(user: str, hid: int, pid: int | None = None) -> bool:
    with Session(_engine) as s:
        h = s.get(Holding, hid)
        if h is None or h.user != _norm(user):
            return False
        if pid is not None and h.portfolio_id != pid:
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


def _time_weighted_100(holds, panel, index) -> list:
    """Growth-of-100 TIME-WEIGHTED return of the portfolio over `index`.

    Rebasing the raw market value is wrong when holdings have different buy
    dates: on the day a new position is bought, its whole value is added to the
    total and the line jumps vertically — that's capital added, not a return.

    This instead compounds a daily portfolio return that is the value-weighted
    average of the daily returns of the holdings held the PREVIOUS day. A
    position bought today enters the weights only tomorrow, so its purchase adds
    no step — making the line directly comparable to a price index.
    """
    cols = {}
    for i, h in enumerate(holds):
        col = panel.get(h["ticker"])
        if col is None or col.dropna().empty:
            continue
        bd = pd.Timestamp(h["buy_date"])
        cols[i] = col.where(col.index >= bd) * float(h["shares"])   # value while held
    if not cols:
        return []
    vmat = pd.DataFrame(cols).reindex(index)
    held_prev = vmat.shift(1)                          # each holding's value at t-1
    wsum = held_prev.sum(axis=1)                       # total held value at t-1
    weights = held_prev.div(wsum, axis=0).replace(
        [float("inf"), float("-inf")], 0.0).fillna(0.0)
    rets = vmat.div(vmat.shift(1)).sub(1.0)            # per-holding daily return (held both days)
    port_ret = (weights * rets.fillna(0.0)).sum(axis=1).fillna(0.0)
    eq = 100.0 * (1.0 + port_ret).cumprod()
    if len(eq):
        eq.iloc[0] = 100.0
    return [round(float(x), 2) for x in eq.values]


def performance(user: str, pid: int | None = None) -> dict:
    holds = list_holdings(user, pid)
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
        # time-weighted growth of 100 (ignores capital added on later buy dates,
        # so no vertical step when a new holding is bought)
        rebased["portfolio"] = _time_weighted_100(holds, panel, total.index)
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


# --------------------------------------------------------------------------- #
# CAPM / MPT analysis + rebalance advice (per portfolio)
# --------------------------------------------------------------------------- #
_ANALYSIS_LB = "2y"          # estimation window for mean/cov/beta
_DRIFT_PP = 7.0              # rebalance when any weight drifts > this many points
_QUARTER_DAYS = 91          # ...or when ~a quarter has passed since earliest buy
_CORR_LBS = ("3mo", "6mo", "1y", "2y")   # selectable correlation windows


def _named_close(holds, lookback):
    """Build a price panel for a portfolio's holdings over `lookback`, with one
    unique display-name column per ticker. Returns (close_df, by_ticker, label,
    cols). Shared by the full analysis and the standalone correlation view."""
    by_ticker: dict[str, dict] = {}
    for h in holds:
        t = h["ticker"]
        by_ticker.setdefault(t, {"name": h["name"] or h["code"] or t,
                                 "code": h["code"], "shares": 0.0,
                                 "buy_date": h["buy_date"]})
        by_ticker[t]["shares"] += float(h["shares"])
        by_ticker[t]["buy_date"] = min(by_ticker[t]["buy_date"], h["buy_date"])

    tickers = list(by_ticker)
    panel = klse_prices.close_panel(tickers, lookback)
    panel.index = pd.to_datetime(panel.index).normalize()
    panel = panel.sort_index().ffill()

    label, seen = {}, {}
    for t in tickers:
        nm = by_ticker[t]["name"]
        if nm in seen:
            nm = f"{nm} ({by_ticker[t]['code'] or t})"
        seen[nm] = True
        label[t] = nm
    cols = [t for t in tickers if t in panel.columns]
    close = panel[cols].rename(columns={t: label[t] for t in cols}) if cols else pd.DataFrame()
    return close, by_ticker, label, cols


def portfolio_correlation(user: str, pid: int | None = None,
                          lookback: str | None = None) -> dict:
    """Stock correlation matrix for a portfolio over a selectable lookback."""
    from . import mpt as mpt_mod
    lb = lookback if lookback in _CORR_LBS else _ANALYSIS_LB
    holds = list_holdings(user, pid)
    if not holds:
        return {"names": [], "matrix": [], "avg": None, "lookback": lb}
    close, _, _, cols = _named_close(holds, lb)
    if not cols:
        return {"names": [], "matrix": [], "avg": None, "lookback": lb}
    res = mpt_mod.corr_matrix(close)
    res["lookback"] = lb
    return res


def analyze_portfolio(user: str, pid: int | None = None) -> dict:
    """CAPM/MPT analysis for one portfolio: risk, Sharpe, beta/alpha, recommended
    rebalance weights (max-Sharpe headline + min-variance alternative), a
    risk-return / efficient-frontier chart payload, and rebalance advice."""
    from . import mpt as mpt_mod

    holds = list_holdings(user, pid)
    if not holds:
        return {"ok": False, "reason": "no holdings"}

    close, by_ticker, label, cols = _named_close(holds, _ANALYSIS_LB)
    if not cols:
        return {"ok": False, "reason": "no price data for these stocks"}

    # current value weights = shares x last price
    last = close.ffill().iloc[-1]
    values = {label[t]: float(by_ticker[t]["shares"]) * float(last.get(label[t], "nan"))
              for t in cols}
    tot_val = sum(v for v in values.values() if v == v)
    wc = [values[c] / tot_val if tot_val else 0.0 for c in close.columns]

    # KLCI daily returns for CAPM beta
    klci_rets = None
    try:
        kl = breadth_mod.index_ohlc("KLCI", _ANALYSIS_LB)
        kl_s = pd.Series(kl["close"], index=pd.to_datetime(kl["dates"]).normalize())
        klci_rets = kl_s.pct_change()
    except Exception:  # noqa: BLE001
        pass

    res = mpt_mod.analyze(close, wc, klci_rets)
    if not res.get("ok"):
        return res

    # rebalance advice: drift of current vs recommended (max-Sharpe) + quarterly
    target = res["max_sharpe"]["weights"]
    cur_w = {s["name"]: s["weight"] for s in res["stocks"]}
    drifts = {nm: abs(cur_w.get(nm, 0.0) - target.get(nm, 0.0)) * 100 for nm in target}
    max_drift = max(drifts.values(), default=0.0)
    earliest = min(v["buy_date"] for v in by_ticker.values())
    days_since = (date.today() - date.fromisoformat(earliest)).days
    reasons = []
    if max_drift > _DRIFT_PP:
        top = max(drifts, key=drifts.get)
        reasons.append(f"{top} is {drifts[top]:.0f} pts off its target weight")
    if days_since >= _QUARTER_DAYS:
        reasons.append(f"~{days_since // 30} months since your earliest buy")
    action = "rebalance" if reasons else "hold"

    res["portfolio_value"] = round(tot_val, 2)
    res["rebalance"] = {
        "action": action,
        "max_drift_pp": round(max_drift, 1),
        "days_since": days_since,
        "reasons": reasons,
        "target": "max_sharpe",
    }
    return res
