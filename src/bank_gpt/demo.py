"""Tenant-scoped mock bank UI; all account reads pass through TenantGateway."""
from __future__ import annotations

import time
import asyncio
from html import escape
from secrets import token_urlsafe
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .tenant import Actor, GATEWAY, verify_token
from .scenarios import SCENARIOS

router = APIRouter()
_results: dict[str, tuple[int, int, str, int]] = {}
_verification: dict[str, bool] = {}


def browser_identity(request: Request) -> tuple[Actor, int]:
    token = request.cookies.get("bankgpt_browser", "")
    try:
        actor, claims = verify_token(token, "bank-gpt-browser")
        tenant_id = int(claims["tenant_id"])
        GATEWAY.require(actor, tenant_id, "balance:read")
        if not claims.get("run_id"):
            raise PermissionError("run-bound browser identity required")
        return actor, tenant_id
    except (PermissionError, KeyError, ValueError, TypeError) as error:
        raise HTTPException(403, "demo session or tenant permission denied") from error


def page(body: str, tenant_id: int = 1) -> HTMLResponse:
    title = "Pine Valley Account Desk" if tenant_id == 2 else "Member Service Console"
    ui_version = "pine-v2" if tenant_id == 2 else "north-v1"
    return HTMLResponse(f"""<!doctype html><html><head><title>{title}</title>
<meta name="bank-ui-version" content="{ui_version}">
<style>body{{font:16px Arial;margin:40px;max-width:850px}}table{{border-collapse:collapse;width:100%}}
td{{border:1px solid #888;padding:12px}}input,button{{font-size:16px;padding:6px}}</style>
</head><body><h1>{title}</h1>{body}</body></html>""")


@router.get("/demo", response_class=HTMLResponse)
@router.get("/demo/pine", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    actor, tenant_id = browser_identity(request)
    if request.url.path == "/demo/pine" and tenant_id != 2:
        raise HTTPException(403, "tenant UI mismatch")
    if request.url.path == "/demo" and tenant_id == 2:
        raise HTTPException(403, "tenant UI mismatch")
    _, claims = verify_token(request.cookies["bankgpt_browser"], "bank-gpt-browser")
    run_id = claims["run_id"]
    fault = SCENARIOS.get(run_id, actor.id, tenant_id, "step_1") or SCENARIOS.get(
        run_id, actor.id, tenant_id, "step_2")
    if fault and fault.fault == "missing_target":
        return page("<p>Lookup is temporarily unavailable</p>", tenant_id)
    if fault and fault.fault == "ambiguous_target":
        return page("<label>Member ID<input name='member_id'></label><label>Member ID<input name='member_id'></label>", tenant_id)
    if fault and fault.fault == "locator_drift":
        return page("""<form action='/demo/search' method='post'>
<label for='lookup-v2'>Customer lookup</label>
<input id='lookup-v2' name='lookup_ref' autocomplete='off' required>
<button type='submit'>Search</button></form>""", tenant_id)
    if tenant_id == 2:
        return page("""<section class='account-search'><h2>Find an account</h2>
<form action='/demo/search' method='post'><label for='pine-reference'>Customer reference</label>
<input id='pine-reference' name='customer_ref' autocomplete='off' required>
<button class='find-account' type='submit'>Find account</button></form></section>""", tenant_id)
    button = '<button type="submit">Search</button>'
    if fault and fault.fault == "risky_control":
        button = '<button type="submit" name="transfer">Search</button>'
    if fault and fault.fault == "disallowed_destination":
        button = '<button type="submit" formaction="https://example.org/outside">Search</button>'
    return page("""<table><tr><td>Member lookup</td><td>
<form action="/demo/search" method="post"><label for="member">Member ID</label>
<input id="member" name="member_id" autocomplete="off" required>
""" + button + "</form></td></tr></table>")


@router.post("/demo/search")
async def search(request: Request) -> RedirectResponse:
    actor, tenant_id = browser_identity(request)
    _, claims = verify_token(request.cookies["bankgpt_browser"], "bank-gpt-browser")
    fault = SCENARIOS.get(claims["run_id"], actor.id, tenant_id, "step_2")
    drift = SCENARIOS.consume(claims["run_id"], actor.id, tenant_id, "step_1")
    if fault and fault.fault == "slow_load":
        SCENARIOS.consume(claims["run_id"], actor.id, tenant_id, "step_2")
        await asyncio.sleep(fault.delay_seconds)
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    member_id = form.get("lookup_ref" if drift and drift.fault == "locator_drift" else
                         "customer_ref" if tenant_id == 2 else "member_id", [""])[0]
    if len(member_id) > 64:
        raise HTTPException(422, "member ID too long")
    now = int(time.time())
    for stale_token, state in list(_results.items()):
        if state[3] < now:
            _results.pop(stale_token, None)
            _verification.pop(stale_token, None)
    token = token_urlsafe(24)
    _results[token] = (actor.id, tenant_id, member_id, now + 300)
    response = RedirectResponse("/demo/result", status_code=303)
    response.set_cookie("demo_session", token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=300, path="/demo")
    return response


@router.get("/demo/result", response_class=HTMLResponse)
async def result(request: Request) -> HTMLResponse:
    actor, tenant_id = browser_identity(request)
    _, claims = verify_token(request.cookies["bankgpt_browser"], "bank-gpt-browser")
    fault = SCENARIOS.consume(claims["run_id"], actor.id, tenant_id, "step_2")
    if fault and fault.fault == "session_expired":
        return page("<p>Session expired</p><a href='/demo'>Start again</a>")
    if fault and fault.fault == "validation_error":
        return page("<p>Invalid member ID</p><a href='/demo'>Start again</a>")
    if fault and fault.fault == "app_error":
        return page("<p>Application error</p><a href='/demo'>Start again</a>")
    token = request.cookies.get("demo_session", "")
    result_state = _results.get(token)
    if result_state is None or result_state[0] != actor.id or result_state[1] != tenant_id or result_state[3] < time.time():
        return page("<p>Session expired</p><a href='/demo'>Start again</a>")
    verification = SCENARIOS.consume(claims["run_id"], actor.id, tenant_id, "step_3")
    if verification and verification.fault == "verification_required":
        _verification[token] = False
    if token in _verification and not _verification[token]:
        return page("<p>Verification required</p><form action='/demo/verify' method='post'>"
                    "<button type='submit'>Verify lookup</button></form>")
    member_id = result_state[2]
    if member_id == "403":
        return page("<p>Permission denied</p><a href='/demo'>Start again</a>")
    record = GATEWAY.balance(actor, tenant_id, member_id)
    if record is None:
        return page("<p>Member not found</p><a href='/demo'>Start again</a>")
    cents = record["balance_cents"]
    balance = f"${cents // 100:,}.{cents % 100:02d}"
    if tenant_id == 2:
        return page(f"""<h2>Account overview</h2><dl>
<dt>Customer reference</dt><dd>{escape(record['member_id'])}</dd>
<dt>Customer</dt><dd>{escape(record['name'])}</dd></dl>
<section class='savings'><span>Savings available</span>
<strong data-role='savings-balance'>{balance}</strong></section>
<p>Review only. No transaction controls are enabled.</p>""", tenant_id)
    return page(f"""<h2>Member detail</h2><table>
<tr><td>Member ID</td><td>{escape(record['member_id'])}</td></tr>
<tr><td>Member name</td><td>{escape(record['name'])}</td></tr>
<tr><td>Savings balance</td><td>{balance}</td></tr></table>
<p>Review only. No transaction controls are enabled.</p>""")


@router.post("/demo/verify")
async def verify(request: Request) -> RedirectResponse:
    actor, tenant_id = browser_identity(request)
    token = request.cookies.get("demo_session", "")
    state = _results.get(token)
    if state is None or state[0] != actor.id or state[1] != tenant_id or state[3] < time.time():
        raise HTTPException(403, "verification session expired")
    if token not in _verification:
        raise HTTPException(409, "verification was not requested")
    _verification[token] = True
    return RedirectResponse("/demo/result", status_code=303)
