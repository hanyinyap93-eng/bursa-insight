"""
Bursa Insight API — FastAPI app.

Endpoints (MVP):
  GET  /                         - service info
  GET  /health                   - liveness
  GET  /api/indices              - index registry
  GET  /api/breadth/overview     - 'market breadth at a glance' (KLCI)
  GET  /api/breadth/series       - Index Health time series + index overlay
  GET  /api/sectors/rotation     - sector rotation heatmap + ranked snapshot
  GET  /api/screener/correlated  - top-N constituents correlated to the index
  GET  /api/sentiment/analyst    - Malaysia analyst sentiment (KLCI constituents)
  GET  /api/gex/klci             - KLCI warrant Gamma Exposure (issuer hedging map)
  GET  /api/vix/klci             - synthetic KLCI VIX (30-day volatility index)
  GET  /api/fbm/indexes          - FBM market-index registry (Mid 70/ACE/EMAS/Fledgling)
  GET  /api/fbm/{key}            - FBM index health + per-sector health %
  GET  /api/risk-appetite        - ACE/MID70/KLCI spreads (Ziemba turn-of-year & size)
  GET  /api/screener/presets     - preset screen definitions
  POST /api/screener/run         - run a custom screen
  GET  /api/news                 - aggregated local + global market news
  GET  /api/alerts               - list alerts
  POST /api/alerts               - create an alert
  DELETE /api/alerts/{id}        - delete an alert
  GET  /api/alerts/evaluate      - evaluate alerts now (which are firing)
  POST /api/backtest/health      - backtest the Index Health timing strategy
  POST /api/backtest/screen      - backtest an equal-weight signal screen
  GET  /api/stock/{ticker}       - per-stock OHLC + indicators for charts
  POST /api/refresh              - force-refresh caches
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .core import alerts as alerts_mod
from .core import auth as auth_mod
from .core import backtest as bt
from .core import breadth as breadth_mod
from .core import index_health as ih
from .core import logins as logins_mod
from .core import mailer as mailer_mod
from .core import news as news_mod
from .core import portfolio as portfolio_mod
from .core import screener as screener_mod
from .core import service
from .core import users as users_mod
from .schemas import (
    AlertRequest, BacktestHealthRequest, BacktestScreenRequest,
    EmailLoginRequest, EmailSignupRequest, GoogleAuthRequest, PortfolioAddRequest,
    PortfolioNameRequest, ResendRequest, ScreenRequest, VerifyRequest,
)

log = logging.getLogger("bursa.api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background scheduler: warms the heavy caches on boot (using the
    on-disk cache if present) and re-warms them on an interval so users always
    hit fresh, pre-built data instead of triggering an on-request rebuild."""
    service.start_scheduler()
    yield


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

# Auth uses Bearer tokens (Authorization header), NOT cookies, so credentials are
# not needed. Sending them alongside a wildcard origin is invalid and makes the
# server reflect any origin — so we disable credentials and only allow "*" when
# no explicit origin list is configured.
_cors_wildcard = settings.cors_origins == "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_wildcard else settings.cors_origins.split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """Never let the browser cache the single-page HTML — otherwise a redeploy
    (e.g. an updated sign-in form) can keep showing a stale page. Static assets
    (JS/images) stay cacheable."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/info")
def info():
    import os
    cache = service.CACHE_DIR
    # cache diagnostics — confirm where the on-disk cache actually lives and
    # that it is writable (used to verify a Render persistent-disk mount)
    files = writable = None
    try:
        files = sum(1 for _ in cache.glob("*")) if cache.exists() else 0
        probe = cache / ".write_probe"
        cache.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
    except Exception:  # noqa: BLE001
        writable = False
    return {"app": settings.app_name, "version": settings.version,
            "docs": "/docs", "indices": list(service.INDEXES),
            "cache_dir": str(cache),
            "cache_dir_env": os.environ.get("BURSA_CACHE_DIR"),
            "cache_files": files, "cache_writable": writable}


@app.get("/health")
def liveness():
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Auth — verify a Google ID token server-side, then mint our own session JWT.
# The gated routes below require that session JWT (see auth_mod.require_auth).
# --------------------------------------------------------------------------- #
_SIGNUP_DOMAINS = {d.strip().lower() for d in settings.signup_email_domains.split(",") if d.strip()}


def _signup_domain_ok(email: str) -> bool:
    """True if this email's domain is allowed to create an email+password account."""
    if not _SIGNUP_DOMAINS:
        return True
    dom = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
    return dom in _SIGNUP_DOMAINS


def _signup_blocked_msg() -> str:
    doms = " or ".join("@" + d for d in sorted(_SIGNUP_DOMAINS))
    return (f"During our beta, email sign-up is limited to {doms} addresses. "
            "You can use “Sign in with Google” with any Google account instead.")


@app.get("/api/config")
def public_config():
    """Public front-end config resolved at deploy time from env vars, so the
    Google client ID isn't frozen into the shipped HTML. The page reads this on
    load and falls back to the built-in <meta> value if it's unreachable."""
    return {"google_client_id": settings.google_client_id,
            "email_auth_enabled": settings.email_auth_enabled,
            "signup_email_domains": sorted(_SIGNUP_DOMAINS)}


def _require_email_auth():
    """Reject email endpoints when email sign-in is switched off (Google-only)."""
    if not settings.email_auth_enabled:
        raise HTTPException(404, "email sign-in is disabled; use Google sign-in")


@app.post("/api/auth/google")
def auth_google(req: GoogleAuthRequest):
    try:
        claims = auth_mod.verify_google_idtoken(req.credential)
    except ValueError as exc:
        raise HTTPException(401, f"Google sign-in rejected: {exc}")
    except Exception as exc:  # noqa: BLE001 - e.g. Google certs unreachable
        raise HTTPException(502, f"could not verify Google token: {exc}")
    logins_mod.record(claims["email"], claims.get("name", ""), "google")
    return auth_mod.make_session_jwt(claims["email"], claims.get("name", ""))


def _verify_link(request: Request, email: str) -> str:
    """Build the confirmation link the user clicks (points at the SPA, which
    posts the token to /api/auth/verify)."""
    base = (settings.public_url or str(request.base_url)).rstrip("/")
    return f"{base}/?verify={auth_mod.make_verify_token(email)}"


def _send_verification(request: Request, email: str) -> dict:
    """Send (or dev-log) the confirmation email; return the signup response.
    In dev mode (no SMTP configured) the link is returned so it can be tested."""
    link = _verify_link(request, email)
    sent = False
    try:
        sent = mailer_mod.send_verification(email, link)
    except Exception as exc:  # noqa: BLE001 - SMTP failure shouldn't 500 the signup
        log.warning("verification email to %s failed: %s", email, exc)
        raise HTTPException(502, f"could not send confirmation email: {exc}")
    resp = {"pending": True, "email": email}
    if not sent:                      # dev mode: expose the link for local testing
        resp["dev_link"] = link
    return resp


@app.post("/api/auth/signup")
def auth_signup(req: EmailSignupRequest, request: Request):
    """Create an UNVERIFIED email+password account and email a confirmation link.
    Returns {pending:true} — no session token until the email is confirmed."""
    _require_email_auth()
    if not _signup_domain_ok(users_mod._norm(req.email)):
        raise HTTPException(400, _signup_blocked_msg())
    try:
        users_mod.create_user(req.email, req.password, req.name or "")
    except ValueError as exc:
        # if the email exists but is unconfirmed, just (re)send the link
        existing = users_mod.get_user(req.email)
        if "already exists" in str(exc) and existing and not existing["verified"]:
            return _send_verification(request, existing["email"])
        raise HTTPException(400, str(exc))
    return _send_verification(request, users_mod._norm(req.email))


@app.post("/api/auth/verify")
def auth_verify(req: VerifyRequest):
    """Confirm an account from the emailed token, then return a session token
    (so clicking the link signs the user straight in)."""
    _require_email_auth()
    try:
        email = auth_mod.read_verify_token(req.token)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    u = users_mod.get_user(email)
    if u is None:
        raise HTTPException(404, "account not found")
    users_mod.mark_verified(email)
    logins_mod.record(u["email"], u["name"], "email")
    return auth_mod.make_session_jwt(u["email"], u["name"])


# --------------------------------------------------------------------------- #
# Admin: who has logged in (owner-only)
# --------------------------------------------------------------------------- #
def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}


@app.get("/api/admin/logins")
def admin_logins(limit: int = Query(500, ge=1, le=5000),
                 user=Depends(auth_mod.require_auth)):
    """List recent successful logins (Google + email), newest first. Restricted
    to the owner emails in BURSA_ADMIN_EMAILS — any other signed-in user gets 403."""
    if (user.get("sub") or "").strip().lower() not in _admin_emails():
        raise HTTPException(403, "admin access required")
    return {"logins": logins_mod.recent(limit)}


@app.post("/api/auth/resend")
def auth_resend(req: ResendRequest, request: Request):
    """Resend the confirmation email. Always returns ok (doesn't reveal whether
    the address is registered); only actually sends for an unverified account."""
    _require_email_auth()
    u = users_mod.get_user(req.email)
    if u is not None and not u["verified"]:
        try:
            mailer_mod.send_verification(u["email"], _verify_link(request, u["email"]))
        except Exception as exc:  # noqa: BLE001 - don't reveal existence; just log
            log.warning("resend to %s failed: %s", u["email"], exc)
    return {"ok": True}


@app.post("/api/auth/login")
def auth_login(req: EmailLoginRequest):
    """Log in with an email+password account and return a session token.
    Refuses unverified accounts (403) so the caller can prompt to confirm."""
    _require_email_auth()
    try:
        u = users_mod.authenticate(req.email, req.password)
    except ValueError as exc:
        raise HTTPException(401, str(exc))
    if not u["verified"]:
        raise HTTPException(403, "Please confirm your email first — check your inbox for the link.")
    logins_mod.record(u["email"], u["name"], "email")
    return auth_mod.make_session_jwt(u["email"], u["name"])


# --------------------------------------------------------------------------- #
# Portfolio (per signed-in user): holdings + buy-and-hold past performance
# --------------------------------------------------------------------------- #
@app.get("/api/portfolio")
def portfolio_list(user=Depends(auth_mod.require_auth)):
    return {"holdings": portfolio_mod.list_holdings(user["sub"])}


@app.post("/api/portfolio")
def portfolio_add(req: PortfolioAddRequest, user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.add_holding(user["sub"], req.code, req.ticker,
                                         req.name or "", req.shares, req.buy_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/portfolio/{hid}")
def portfolio_remove(hid: int, user=Depends(auth_mod.require_auth)):
    if not portfolio_mod.remove_holding(user["sub"], hid):
        raise HTTPException(404, "holding not found")
    return {"deleted": hid}


@app.get("/api/portfolio/performance")
def portfolio_performance(user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.performance(user["sub"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"portfolio performance failed: {exc}")


# --------------------------------------------------------------------------- #
# Multiple named portfolios (up to 3) with CAPM/MPT analysis + rebalance advice
# --------------------------------------------------------------------------- #
@app.get("/api/portfolios")
def portfolios_list(user=Depends(auth_mod.require_auth)):
    return {"portfolios": portfolio_mod.list_portfolios(user["sub"])}


@app.post("/api/portfolios")
def portfolios_create(req: PortfolioNameRequest, user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.create_portfolio(user["sub"], req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/portfolios/{pid}")
def portfolios_rename(pid: int, req: PortfolioNameRequest,
                      user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.rename_portfolio(user["sub"], pid, req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/portfolios/{pid}")
def portfolios_delete(pid: int, user=Depends(auth_mod.require_auth)):
    if not portfolio_mod.delete_portfolio(user["sub"], pid):
        raise HTTPException(404, "portfolio not found")
    return {"deleted": pid}


@app.get("/api/portfolios/{pid}/holdings")
def portfolio_holdings(pid: int, user=Depends(auth_mod.require_auth)):
    return {"holdings": portfolio_mod.list_holdings(user["sub"], pid)}


@app.post("/api/portfolios/{pid}/holdings")
def portfolio_holdings_add(pid: int, req: PortfolioAddRequest,
                           user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.add_holding(user["sub"], req.code, req.ticker,
                                         req.name or "", req.shares, req.buy_date, pid=pid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/portfolios/{pid}/holdings/{hid}")
def portfolio_holdings_remove(pid: int, hid: int, user=Depends(auth_mod.require_auth)):
    if not portfolio_mod.remove_holding(user["sub"], hid, pid=pid):
        raise HTTPException(404, "holding not found")
    return {"deleted": hid}


@app.get("/api/portfolios/{pid}/performance")
def portfolio_perf_scoped(pid: int, user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.performance(user["sub"], pid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"portfolio performance failed: {exc}")


@app.get("/api/portfolios/{pid}/analysis")
def portfolio_analysis(pid: int, user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.analyze_portfolio(user["sub"], pid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"portfolio analysis failed: {exc}")


@app.get("/api/portfolios/{pid}/correlation")
def portfolio_correlation(pid: int, lookback: str = "2y",
                          user=Depends(auth_mod.require_auth)):
    try:
        return portfolio_mod.portfolio_correlation(user["sub"], pid, lookback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"portfolio correlation failed: {exc}")


# --------------------------------------------------------------------------- #
# Indices & breadth
# --------------------------------------------------------------------------- #
@app.get("/api/indices")
def indices():
    return [{"id": k, **v} for k, v in service.INDEXES.items()]


@app.get("/api/breadth/overview")
def breadth_overview(index: str = "KLCI", lookback: str = "1y", corr_window: str = None,
                     term: str = "short"):
    try:
        return breadth_mod.breadth_overview(index, lookback, corr_window, term)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"breadth compute failed: {exc}")


@app.get("/api/breadth/series")
def breadth_series(index: str = "KLCI", lookback: str = "1y", term: str = "short"):
    try:
        return breadth_mod.health_series(index, lookback, term)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"series compute failed: {exc}")


@app.get("/api/index/{key}/ohlc")
def index_ohlc(key: str, lookback: str = "5y"):
    """OHLC for an index (KLCI or a sector key) so it can be charted."""
    try:
        return breadth_mod.index_ohlc(key, lookback)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"index ohlc failed: {exc}")


@app.get("/api/correlations")
def correlations(ticker: str, lookback: str = "6mo"):
    """Correlation of a stock to every index (KLCI + 13 sectors)."""
    try:
        return breadth_mod.stock_index_correlations(ticker, lookback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"correlations failed: {exc}")


@app.get("/api/search")
def search(q: str, limit: int = 12):
    """Search the whole Bursa market by stock code or ticker name/symbol."""
    from .core import klse_quotes
    query = (q or "").strip().upper()
    if not query:
        return []
    # indexes (KLCI + sectors) match first
    index_names = {"KLCI": "FBM KLCI", **breadth_mod.SECTOR_DISPLAY}
    idx_matches = [
        {"code": k, "name": f"{nm} index", "ticker": k, "is_index": True}
        for k, nm in index_names.items()
        if query in k.upper() or query in nm.upper()
    ]
    try:
        quotes = klse_quotes.get_quotes()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"search failed: {exc}")
    import re as _re
    clean = lambda s: _re.sub(r"\s*\[[^\]]*\]\s*$", "", s or "").strip()
    out = []
    for code, rec in quotes.items():
        name = (rec.get("name") or "").upper()
        digits = code.upper()
        if query == digits or digits.startswith(query) or query in name:
            out.append({
                "code": code, "name": clean(rec.get("name")), "ticker": f"{code}.KL",
                "last": rec.get("last"), "chg_pct": rec.get("chg_pct"),
                "sector": rec.get("sector"),
            })
    out.sort(key=lambda r: (
        r["code"].upper() != query,                       # exact code first
        not (r["name"] or "").upper().startswith(query),   # name prefix next
        not r["code"].upper().startswith(query),           # code prefix
        r["name"] or "",
    ))
    return (idx_matches + out)[:limit]   # indexes listed first


@app.get("/api/quotes")
def quotes(index: str = "KLCI", lookback: str = "1y"):
    try:
        return breadth_mod.quotes(index, lookback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"quotes failed: {exc}")


@app.get("/api/sector/{key}")
def sector_detail(key: str, lookback: str = "1y", corr_window: str = None,
                  term: str = "short"):
    try:
        return breadth_mod.sector_detail(key, lookback, corr_window, term)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"sector detail failed: {type(exc).__name__}: {exc}")


@app.get("/api/sectors/rotation")
def sector_rotation(lookback: str = "1y", term: str = "short"):
    try:
        return breadth_mod.sector_rotation(lookback, term)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"sector rotation failed: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Screener
# --------------------------------------------------------------------------- #
@app.get("/api/screener/correlated")
def screener_correlated(index: str = "KLCI", lookback: str = "1y",
                        top: int = Query(10, ge=1, le=100),
                        window: int = Query(None)):
    try:
        result = service.get_health(index, lookback)
        return screener_mod.correlated_constituents(result, top=top, window=window)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"correlation screen failed: {exc}")


@app.get("/api/screener/presets")
def screener_presets():
    return {name: vars(c) for name, c in screener_mod.PRESETS.items()}


@app.post("/api/screener/run")
def screener_run(req: ScreenRequest):
    try:
        result = service.get_health(req.index, req.lookback)
        criteria = screener_mod.ScreenCriteria(
            above_sma=req.above_sma, momentum_up=req.momentum_up,
            rsi_overbought=req.rsi_overbought, rsi_oversold=req.rsi_oversold,
            new_high=req.new_high, new_low=req.new_low,
            min_correlation=req.min_correlation, min_return_pct=req.min_return_pct,
            max_return_pct=req.max_return_pct, sectors=req.sectors,
            healthy_only=req.healthy_only,
        )
        rows = screener_mod.screen(result, criteria, corr_window=req.corr_window)
        return {"count": len(rows), "results": rows}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"screen failed: {exc}")


# --------------------------------------------------------------------------- #
# Analyst sentiment & warrant GEX (KLCI)
# --------------------------------------------------------------------------- #
@app.get("/api/sentiment/analyst", dependencies=[Depends(auth_mod.require_auth)])
def analyst_sentiment(index: str = "KLCI", force: bool = False):
    """Malaysia analyst sentiment over the KLCI constituents: per-stock
    recommendation counts + -1..+1 scores and the overall gauge. Never blocks:
    serves the cache or returns {warming:true} (+ the last build error, e.g.
    Yahoo blocking the host's IP) while building in the background."""
    if index != "KLCI":
        raise HTTPException(404, f"analyst sentiment not available for {index}")
    try:
        r = service.get_analyst_sentiment(index, nowait=True)
        if r is not None:
            return r
        return {"warming": True, "error": service.build_error(f"sentiment:{index}")}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"analyst sentiment failed: {exc}")


@app.get("/api/fundflow", dependencies=[Depends(auth_mod.require_auth)])
def fund_flow(force: bool = False):
    """KLCI 30 tick-rule fund flow (1-month net buy/sell per constituent, per
    sector, and the latest day ranked). Never blocks: serves the cached payload
    or {warming:true} while it builds in the background."""
    try:
        r = service.get_fund_flow(nowait=True)
        if r is not None:
            return r
        return {"warming": True, "error": service.build_error("fundflow:KLCI")}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"fund flow failed: {exc}")


@app.get("/api/gex/klci", dependencies=[Depends(auth_mod.require_auth)])
def klci_gex(force: bool = False):
    """KLCI index-warrant Gamma Exposure: per-warrant issuer GEX, by-strike
    aggregation, net-GEX profile with the gamma trough, and the Index Health x
    GEX regime readout. The warrant-chain scrape is slow, so this never blocks:
    it serves the cached payload or returns {warming:true} while it builds in
    the background (12h TTL, stale-while-revalidate)."""
    try:
        r = service.get_klci_gex(nowait=True)
        if r is not None:
            return r
        return {"warming": True, "error": service.build_error("gex:KLCI")}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"GEX compute failed: {exc}")


@app.get("/api/vix/klci", dependencies=[Depends(auth_mod.require_auth)])
def klci_vix(force: bool = False):
    """Synthetic KLCI VIX — 30-day volatility index (Yang-Zhang realized blended
    with an EWMA conditional-vol proxy), annualized %, with 10th/90th-percentile
    FEAR / COMPLACENCY regime bands over the available history. Cheap build, but
    served stale-while-revalidate (3h TTL); returns {warming:true} on a cold
    cache."""
    try:
        r = service.get_klci_vix(nowait=True)
        if r is not None:
            return r
        return {"warming": True, "error": service.build_error("vix:KLCI")}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"VIX compute failed: {exc}")


# --------------------------------------------------------------------------- #
# FBM market indexes (Mid 70 / ACE / EMAS / Fledgling)
# --------------------------------------------------------------------------- #
@app.get("/api/risk-appetite", dependencies=[Depends(auth_mod.require_auth)])
def risk_appetite(lookback: str = "1y", force: bool = False):
    """Risk appetite: FBM ACE / MID 70 / KLCI index spreads (Ziemba
    turn-of-year & size effect) — H scores, rolling betas, monthly
    seasonality with t-stats, and rebased relative performance. `lookback`
    (3mo/6mo/1y/2y) sets the health-score z-score standardisation window."""
    try:
        r = service.get_risk_appetite(lookback, nowait=True)
        if r is not None:
            return r
        return {"warming": True, "error": service.build_error(f"riskapp:{lookback}")}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"risk appetite failed: {exc}")


@app.get("/api/risk-appetite/correlation", dependencies=[Depends(auth_mod.require_auth)])
def risk_appetite_correlation(lookback: str = "1y"):
    """Correlation matrix of the four risk-appetite health-score series
    (ACE-KLCI, 70-KLCI, ACE-70, H_RiskAppetite) over the given lookback."""
    try:
        r = service.get_ra_correlation(lookback, nowait=True)
        if r is not None:
            return r
        return {"warming": True, "error": service.build_error(f"riskappcorr:{lookback}")}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"risk appetite correlation failed: {exc}")


@app.get("/api/fbm/indexes", dependencies=[Depends(auth_mod.require_auth)])
def fbm_indexes():
    """Registry of the FBM market indexes served by /api/fbm/{key}."""
    from .core import fbm_indexes as fbm_mod
    return [{"key": k, **v} for k, v in fbm_mod.FBM_INDEXES.items()]


@app.get("/api/fbm/{key}", dependencies=[Depends(auth_mod.require_auth)])
def fbm_health(key: str, lookback: str = "1y", term: str = "short", force: bool = False):
    """Index Health + per-sector Health % for one FBM market index.
    term: short (10/25) | mid (20/50) | long (50/100). The constituent scrape +
    price download is slow, so this never blocks: it serves the cached payload
    or returns {warming:true} while it builds in the background (8h TTL)."""
    try:
        r = service.get_fbm_health(key, lookback, term, nowait=True)
        if r is not None:
            return r
        return {"warming": True, "key": key.upper(),
                "error": service.build_error(f"fbm:{key.upper()}:{lookback}:{term}")}
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"FBM health failed: {exc}")


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #
@app.get("/api/news")
def news(scope: str = None, sector: str = None, index: str = None,
         limit: int = Query(50, ge=1, le=200)):
    return news_mod.get_news(scope=scope, sector=sector, index=index, limit=limit)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
@app.get("/api/alerts")
def list_alerts():
    return [a.to_dict() for a in alerts_mod.list_alerts()]


@app.post("/api/alerts")
def create_alert(req: AlertRequest):
    try:
        a = alerts_mod.create_alert(req.metric, req.op, req.threshold, req.label)
        return a.to_dict()
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: int):
    if not alerts_mod.delete_alert(alert_id):
        raise HTTPException(404, "alert not found")
    return {"deleted": alert_id}


@app.get("/api/alerts/evaluate")
def evaluate_alerts():
    try:
        return {"firing": alerts_mod.evaluate()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"evaluate failed: {exc}")


# --------------------------------------------------------------------------- #
# Backtests
# --------------------------------------------------------------------------- #
@app.post("/api/backtest/health")
def backtest_health(req: BacktestHealthRequest):
    try:
        return bt.backtest_health_threshold(
            req.index, req.lookback, req.entry, req.exit_, req.cost_bps)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"backtest failed: {exc}")


@app.post("/api/backtest/screen")
def backtest_screen(req: BacktestScreenRequest):
    try:
        return bt.backtest_signal_screen(
            req.index, req.lookback, req.require_above_sma,
            req.require_momentum_up, req.cost_bps)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"backtest failed: {exc}")


# --------------------------------------------------------------------------- #
# Per-stock chart data
# --------------------------------------------------------------------------- #
@app.get("/api/stock/{ticker}")
def stock(ticker: str, lookback: str = "1y"):
    """OHLC + SMA/RSI for a single stock (charts). ticker e.g. '1155.KL'."""
    try:
        # PRIMARY: klsescreener UDF (works from cloud); FALLBACK: yfinance.
        raw = None
        try:
            from .core import klse_prices
            raw = klse_prices.history(ticker, lookback=lookback)
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None or raw.empty:
            import yfinance as yf
            # cap the fallback so an unknown/slow ticker can't hang the request
            raw = yf.Ticker(ticker).history(period=lookback, interval="1d",
                                            auto_adjust=False, timeout=8)
        if raw is None or raw.empty:
            raise HTTPException(404, f"no data for {ticker}")
        if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
            raw.columns = raw.columns.get_level_values(0)
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()
        close = raw["Close"]
        out = {
            "ticker": ticker,
            "dates": [str(d.date()) for d in raw.index],
            "open": [round(float(x), 4) for x in raw["Open"]],
            "high": [round(float(x), 4) for x in raw["High"]],
            "low": [round(float(x), 4) for x in raw["Low"]],
            "close": [round(float(x), 4) for x in close],
            "volume": [int(x) if not _isnan(x) else 0 for x in raw["Volume"]],
            "sma10": [_r(x) for x in ih.sma(close, 10)],
            "rsi10": [_r(x) for x in ih.rsi(close, 10)],
        }
        # fundamentals (PE, P/B, DY, EPS, ...) from the whole-market klsescreener data
        try:
            from .core import klse_quotes
            code = ticker.split(".")[0]
            f = klse_quotes.get_one(code) or {}
            out["fundamentals"] = {k: f.get(k) for k in
                                   ("pe", "pb", "dy", "eps", "nta", "market_cap", "volume")}
        except Exception:  # noqa: BLE001 - fundamentals are best-effort
            out["fundamentals"] = {}
        return out
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"stock fetch failed: {exc}")


@app.post("/api/refresh")
def refresh():
    service.refresh()
    return {"status": "refreshed"}


def _isnan(x):
    try:
        return x != x
    except Exception:  # noqa: BLE001
        return True


def _r(x):
    return None if _isnan(x) else round(float(x), 4)


# --------------------------------------------------------------------------- #
# Serve the frontend (single deployable unit). Mounted LAST so the /api routes
# above always take precedence. Visiting "/" serves frontend/index.html.
# --------------------------------------------------------------------------- #
from pathlib import Path  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
