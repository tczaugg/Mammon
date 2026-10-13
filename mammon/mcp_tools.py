"""mammon.mcp_tools -- the read-only tool surface an LLM sees (roadmap item 3).

Every function here takes an open connection and plain arguments, and returns
a JSON-serializable dict. No MCP types, no transport: :mod:`mammon.mcp_server`
binds these to the protocol, and the tests call them directly. Three rules
shape the surface:

* **Aggregates first.** The default answer to "what did I spend" is a report
  row set, not transactions; the row listing and the SQL tool exist for the
  long tail and are bounded by a ``limit`` so a tool answer stays small.
* **Nothing identifying leaves.** Account numbers, bank login URLs and saved
  download inputs are never returned, by any tool, including the SQL tool
  (an authorizer blanks those columns).
* **Money is a decimal dollar string** (``"-1234.56"``), negative = money out,
  never a float. Dates are ISO ``YYYY-MM-DD``. Accounts may be named by id or
  by name; categories by ``Parent:Child`` path.

The tools read through the same domain functions the app uses
(:mod:`mammon.ledger`, :mod:`mammon.reports`, :mod:`mammon.investments`,
:mod:`mammon.loans`, :mod:`mammon.scheduled`), so an LLM composes the numbers
the register and the report windows show, never a re-derivation of them.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable, Optional

from mammon import (budgets, category_types, crypto, investments, ledger,
                    loans, portfolio, rebalance, retirement, scheduled,
                    security_mix)
from mammon.reports import balances as _balances
from mammon.reports import budget as _budget
# Import the function straight from the submodule: the reports package re-exports
# a function named ``investment_performance``, which shadows the submodule of the
# same name in the package namespace, so ``from mammon.reports import
# investment_performance`` would bind the function, not the module.
from mammon.reports.investment_performance import (
    investment_performance as _invperf,
)
from mammon.reports import flows as _flows
from mammon.reports import listing as _listing
from mammon.reports import payees as _payees
from mammon.reports import spending as _spending

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000

# Columns no tool may return. Two different reasons, one mechanism:
#
#   * the ``accounts`` three identify the user to a third party - an account
#     number, the institution's URL and the download configuration together are
#     enough to go log in as them;
#   * the ``people`` health columns are nobody's business but the user's. He
#     agreed to store them at all only on the condition that a connected LLM be
#     structurally incapable of reading them, which is this line, not a policy
#     sentence in a prompt. They come from ``retirement.HEALTH_COLUMNS`` rather
#     than being retyped here so that adding a health column to the schema
#     denies it in the same edit; ``test_mcp_tools`` fails if a column on
#     ``people`` is neither classified as health nor as plain.
#
# Denied twice over: ``schema`` omits them so a model never learns the names,
# and the SQLite authorizer returns SQLITE_IGNORE so a model that guesses a
# name anyway reads NULL.
SENSITIVE_COLUMNS = {
    ("accounts", "account_number"),
    ("accounts", "url"),
    ("accounts", "download_config"),
} | {("people", c) for c in retirement.HEALTH_COLUMNS}


# ---------------------------------------------------------------------------
# small conversions
# ---------------------------------------------------------------------------
def dollars(cents: Optional[int]) -> Optional[str]:
    """Integer cents -> ``"-1234.56"`` (no grouping, so it parses anywhere)."""
    if cents is None:
        return None
    c = int(cents)
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"


def cents_of(value) -> Optional[int]:
    """A dollars argument (``"12.50"``, ``12.5``, ``12``) -> cents; None stays None."""
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ValueError(f"not an amount: {value!r}")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _today() -> str:
    return _dt.date.today().isoformat()


def _date(value: str, name: str) -> str:
    try:
        return _dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")


def _limit(limit) -> int:
    n = DEFAULT_LIMIT if limit is None else int(limit)
    return max(1, min(n, MAX_LIMIT))


def _account_rows(conn) -> list:
    return list(ledger.list_accounts(conn, include_closed=True, include_hidden=True))


def _account_public(a) -> dict:
    keys = a.keys()
    return {
        "id": int(a["id"]),
        "name": a["name"],
        "type": a["type"] or "",
        "institution": (a["institution"] if "institution" in keys else None) or "",
        "hidden": bool(a["hidden"]) if "hidden" in keys else False,
        "closed": bool(a["closed_flag"]) if "closed_flag" in keys else False,
    }


def resolve_accounts(conn, accounts) -> Optional[list[int]]:
    """``None`` -> None (every account, hidden excluded by the report); a
    list of ids and/or names (case-insensitive) -> ids. An unknown name is an
    error that lists the names that exist."""
    if accounts is None:
        return None
    if isinstance(accounts, (str, int)):
        accounts = [accounts]
    rows = _account_rows(conn)
    by_id = {int(a["id"]): int(a["id"]) for a in rows}
    by_name = {a["name"].strip().lower(): int(a["id"]) for a in rows}
    out: list[int] = []
    for item in accounts:
        if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
            aid = int(item)
            if aid not in by_id:
                raise ValueError(f"no account with id {aid}")
            out.append(aid)
            continue
        key = str(item).strip().lower()
        if key not in by_name:
            names = ", ".join(sorted(a["name"] for a in rows))
            raise ValueError(f"no account named {item!r}; accounts: {names}")
        out.append(by_name[key])
    return out


def resolve_account(conn, account) -> int:
    ids = resolve_accounts(conn, [account])
    return ids[0]


def resolve_categories(conn, categories) -> Optional[list[int]]:
    """Category ids from paths (``Parent:Child``, case-insensitive) or ids."""
    if categories is None:
        return None
    if isinstance(categories, (str, int)):
        categories = [categories]
    cats = ledger.list_categories(conn, include_hidden=True)
    by_path = {c["path"].lower(): int(c["id"]) for c in cats}
    ids = {int(c["id"]) for c in cats}
    out = []
    for item in categories:
        if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
            cid = int(item)
            if cid not in ids:
                raise ValueError(f"no category with id {cid}")
            out.append(cid)
            continue
        key = str(item).strip().lower()
        if key not in by_path:
            raise ValueError(f"no category {item!r}; use list_categories for the paths")
        out.append(by_path[key])
    return out


# ---------------------------------------------------------------------------
# orientation
# ---------------------------------------------------------------------------
def overview(conn) -> dict:
    """Where to start: today's date, the ledger's date span, net worth, and the
    accounts. Call this before anything else so date ranges are grounded."""
    lo, hi = ledger.transaction_date_bounds(conn)
    accts = [a for a in _account_rows(conn)]
    visible = [a for a in accts if not (("hidden" in a.keys()) and a["hidden"])]
    return {
        "today": _today(),
        "first_transaction": lo,
        "last_transaction": hi,
        "net_worth": dollars(ledger.net_worth(conn)),
        "accounts": [_account_public(a) for a in visible],
        "hidden_accounts": len(accts) - len(visible),
        "conventions": {
            "amounts": "decimal dollar strings; negative = money out",
            "dates": "ISO YYYY-MM-DD, inclusive ranges",
            "accounts": "refer to accounts by name or id; hidden accounts are excluded unless include_hidden is true",
            "categories": "Parent:Child paths",
        },
    }


def list_accounts(conn, include_hidden: bool = False) -> dict:
    """Accounts with their current balances (investments at market)."""
    rep = _balances.account_balances(conn, None, include_hidden=include_hidden)
    rows = []
    for r in rep.rows:
        rows.append({"id": r.account_id, "name": r.name, "type": r.type,
                     "hidden": r.hidden, "closed": r.closed, "balance": dollars(r.cents)})
    return {"accounts": rows, "total": dollars(rep.total)}


def list_categories(conn) -> dict:
    """Every category path with its income/expense classification."""
    grouped = category_types.group_by_type(conn, include_hidden=True)
    return {
        "income": [c["path"] for c in grouped["income"]],
        "expense": [c["path"] for c in grouped["expense"]],
    }


def net_worth(conn, as_of: Optional[str] = None, include_hidden: bool = False) -> dict:
    """Net worth on a date (default: latest), investments valued at market."""
    d = _date(as_of, "as_of") if as_of else None
    return {"as_of": d or "latest",
            "net_worth": dollars(ledger.net_worth(conn, as_of=d, include_hidden=include_hidden))}


# ---------------------------------------------------------------------------
# balances
# ---------------------------------------------------------------------------
def account_balances(conn, as_of: Optional[str] = None, include_hidden: bool = False) -> dict:
    """Every account's balance on a date, with the total."""
    d = _date(as_of, "as_of") if as_of else None
    rep = _balances.account_balances(conn, d, include_hidden=include_hidden)
    return {"as_of": d or "latest",
            "accounts": [{"id": r.account_id, "name": r.name, "type": r.type,
                          "balance": dollars(r.cents)} for r in rep.rows],
            "total": dollars(rep.total)}


def balances_over_time(conn, start: str, end: str, bucket: str = "month",
                       accounts=None, include_hidden: bool = False) -> dict:
    """Each account's balance at the end of every month/quarter/year in the
    range, plus the total per bucket."""
    s, e = _date(start, "start"), _date(end, "end")
    series = _balances.balances_over_time(
        conn, s, e, bucket=bucket, account_ids=resolve_accounts(conn, accounts),
        include_hidden=include_hidden)
    names = {a["id"]: a["name"] for a in series.accounts}
    return {"start": s, "end": e, "bucket": bucket,
            "accounts": [a["name"] for a in series.accounts],
            "samples": [{"bucket": smp.bucket, "as_of": smp.as_of,
                         "balances": {names.get(aid, str(aid)): dollars(c)
                                      for aid, c in smp.by_account.items()},
                         "total": dollars(smp.total)} for smp in series.samples]}


# ---------------------------------------------------------------------------
# flows
# ---------------------------------------------------------------------------
def _flow_rows(rows, buckets) -> list[dict]:
    out = []
    for r in rows:
        d = {"category": r.path, "type": r.type, "total": dollars(r.total)}
        if buckets != ["total"]:
            d["by_bucket"] = {k: dollars(v) for k, v in r.by_bucket.items()}
        out.append(d)
    return out


def income_expense(conn, start: str, end: str, bucket: str = "month",
                   accounts=None, include_hidden: bool = False) -> dict:
    """Every category's net per month/quarter/year/total: income rows, expense
    rows, per-bucket totals and net. Transfers excluded; a refund shrinks its
    expense category."""
    s, e = _date(start, "start"), _date(end, "end")
    rep = _flows.income_expense(conn, s, e, bucket=bucket,
                                account_ids=resolve_accounts(conn, accounts),
                                include_hidden=include_hidden)
    return {"start": s, "end": e, "bucket": bucket, "buckets": rep.buckets,
            "income": _flow_rows(rep.income, rep.buckets),
            "expense": _flow_rows(rep.expense, rep.buckets),
            "income_by_bucket": {k: dollars(v) for k, v in rep.income_by_bucket.items()},
            "expense_by_bucket": {k: dollars(v) for k, v in rep.expense_by_bucket.items()},
            "net_by_bucket": {k: dollars(v) for k, v in rep.net_by_bucket.items()},
            "total_income": dollars(rep.total_income),
            "total_expense": dollars(rep.total_expense),
            "net": dollars(rep.net)}


def cash_flow(conn, start: str, end: str, accounts=None, include_hidden: bool = False) -> dict:
    """Inflows and outflows by category over the window, plus transfers to and
    from accounts outside the selected ones (empty when every account is in)."""
    s, e = _date(start, "start"), _date(end, "end")
    rep = _flows.cash_flow(conn, s, e, account_ids=resolve_accounts(conn, accounts),
                           include_hidden=include_hidden)
    return {"start": s, "end": e,
            "inflows": _flow_rows(rep.income, ["total"]),
            "outflows": _flow_rows(rep.expense, ["total"]),
            "transfers": [{"account": t.name, "net": dollars(t.cents)} for t in rep.transfers],
            "total_inflows": dollars(rep.total_income),
            "total_outflows": dollars(rep.total_expense),
            "net_transfers": dollars(rep.net_transfers),
            "net": dollars(rep.net)}


def compare_periods(conn, a_start: str, a_end: str, b_start: str, b_end: str,
                    accounts=None, include_hidden: bool = False) -> dict:
    """Category nets in period A against period B with the change in dollars
    and percent (percent is null when A was zero)."""
    a = (_date(a_start, "a_start"), _date(a_end, "a_end"))
    b = (_date(b_start, "b_start"), _date(b_end, "b_end"))
    rep = _flows.compare_periods(conn, a[0], a[1], b[0], b[1],
                                 account_ids=resolve_accounts(conn, accounts),
                                 include_hidden=include_hidden)
    return {"a": {"start": a[0], "end": a[1]}, "b": {"start": b[0], "end": b[1]},
            "rows": [{"category": r.path, "type": r.type, "a": dollars(r.a_cents),
                      "b": dollars(r.b_cents), "change": dollars(r.delta),
                      "percent": r.pct} for r in rep.rows],
            "totals": [{"label": t.label, "a": dollars(t.a_cents), "b": dollars(t.b_cents),
                        "change": dollars(t.delta), "percent": t.pct} for t in rep.totals]}


def category_averages(conn, start: str, end: str, bucket: str = "month",
                      accounts=None, include_hidden: bool = False) -> dict:
    """Average per month/quarter/year for every category over the range; the
    divisor is every bucket the range spans, quiet ones included."""
    s, e = _date(start, "start"), _date(end, "end")
    rep = _flows.category_averages(conn, s, e, bucket=bucket,
                                   account_ids=resolve_accounts(conn, accounts),
                                   include_hidden=include_hidden)
    return {"start": s, "end": e, "bucket": bucket, "buckets": rep.n_buckets,
            "rows": [{"category": r.path, "type": r.type, "total": dollars(r.total),
                      "average": dollars(r.average)} for r in rep.rows],
            "average_income": dollars(rep.average_income),
            "average_expense": dollars(rep.average_expense),
            "average_net": dollars(rep.average_net)}


def spending_by_category(conn, start: str, end: str, accounts=None) -> dict:
    """Money OUT by category with parents rolled up over children (positive
    magnitudes; income and refunds are not netted against it)."""
    s, e = _date(start, "start"), _date(end, "end")
    rep = _spending.spending_by_category(conn, s, e, resolve_accounts(conn, accounts))
    return {"start": s, "end": e,
            "rows": [{"category": r.path, "depth": r.depth, "own": dollars(r.own_cents),
                      "total": dollars(r.total_cents)} for r in rep.flat()],
            "total": dollars(rep.total_cents)}


def by_payee(conn, start: str, end: str, direction: str = "net", accounts=None,
             include_hidden: bool = False, limit: Optional[int] = 50) -> dict:
    """Money by payee: ``net`` (the default, signed the way the register is --
    negative = money out), ``out`` (magnitudes of what you paid them) or ``in``
    (what they paid you). Largest by magnitude first; ``count`` is transactions.

    A split transaction counts at its own amount, so a paycheck lands on the
    employer as the DEPOSIT, not as its withholding legs -- and a payee who only
    ever pays you has no ``out`` total at all. ``net`` is the default because
    half a household's payees pay IN (an employer, a pension, a tenant, a
    marketplace that both bills and remits), and it answers both directions in
    one signed number: pay someone $200, take $50 back, and they read
    ``-150.00``."""
    s, e = _date(start, "start"), _date(end, "end")
    rep = _payees.by_payee(conn, s, e, account_ids=resolve_accounts(conn, accounts),
                           include_hidden=include_hidden, direction=direction)
    n = _limit(limit)
    return {"start": s, "end": e, "direction": direction,
            "rows": [{"payee": r.name, "count": r.count, "amount": dollars(r.cents)}
                     for r in rep.rows[:n]],
            "truncated": len(rep.rows) > n, "total": dollars(rep.total)}


def by_tag(conn, start: str, end: str, direction: str = "out", accounts=None,
           include_hidden: bool = False, limit: Optional[int] = 50) -> dict:
    """Money by tag, same shape as by_payee; untagged money is ``(no tag)``."""
    s, e = _date(start, "start"), _date(end, "end")
    rep = _payees.by_tag(conn, s, e, account_ids=resolve_accounts(conn, accounts),
                         include_hidden=include_hidden, direction=direction)
    n = _limit(limit)
    return {"start": s, "end": e, "direction": direction,
            "rows": [{"tag": r.name, "count": r.count, "amount": dollars(r.cents)}
                     for r in rep.rows[:n]],
            "truncated": len(rep.rows) > n, "total": dollars(rep.total)}


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
def transactions(conn, start: str, end: str, accounts=None, categories=None,
                 payee: Optional[str] = None, memo: Optional[str] = None,
                 tag: Optional[str] = None, min_amount=None, max_amount=None,
                 cleared: str = "any", include_transfers: bool = True,
                 include_hidden: bool = False, newest_first: bool = False,
                 limit: Optional[int] = DEFAULT_LIMIT) -> dict:
    """The transactions matching every filter given (a category includes its
    subtree; amounts bound the absolute value in dollars). Bounded by
    ``limit``; ``truncated`` says whether more matched."""
    s, e = _date(start, "start"), _date(end, "end")
    rep = _listing.transactions(
        conn, s, e, account_ids=resolve_accounts(conn, accounts),
        include_hidden=include_hidden, category_ids=resolve_categories(conn, categories),
        payee_contains=payee, memo_contains=memo, tag=tag,
        amount_min=cents_of(min_amount), amount_max=cents_of(max_amount),
        cleared=cleared, include_transfers=include_transfers,
        newest_first=newest_first, limit=_limit(limit))
    rows = []
    for r in rep.rows:
        rows.append({k: r[k] for k in ("id", "date", "account", "num", "payee", "category",
                                       "memo", "tag", "cleared", "reconciled", "is_split")}
                    | {"amount": dollars(r["amount"]), "transfer_account": r["transfer_account"]})
    return {"start": s, "end": e, "count": rep.count, "truncated": rep.truncated,
            "total": dollars(rep.total_cents), "rows": rows}


def search(conn, query: str, account=None, limit: Optional[int] = DEFAULT_LIMIT) -> dict:
    """Free-text search over payee, memo, tag, num, category, account, date and
    amount, cash and investment rows alike, newest first."""
    aid = resolve_account(conn, account) if account is not None else None
    hits = ledger.search_transactions(conn, query, account_id=aid)
    n = _limit(limit)
    rows = []
    for h in hits[:n]:
        row = {"id": h.get("id"), "date": h.get("date"), "account": h.get("account_name"),
               "payee": h.get("payee") or h.get("symbol") or "",
               "category": h.get("category_label") or h.get("action") or "",
               "memo": h.get("memo") or "", "amount": dollars(h.get("amount"))}
        if h.get("symbol"):
            row["symbol"] = h.get("symbol")
            row["action"] = h.get("action")
        rows.append(row)
    return {"query": query, "count": len(rows), "truncated": len(hits) > n, "rows": rows}


# ---------------------------------------------------------------------------
# investments, loans, scheduled
# ---------------------------------------------------------------------------
def _instrument(conn, symbol) -> dict:
    """``{"kind": ..., "option": ...}`` for one symbol -- WHAT the position is,
    added to every per-security payload so the model never has to infer an
    instrument from the shape of its ticker (SRD 5.8e-2, 7.3).

    ``kind`` is ``securities.kind`` verbatim (``"option"``, ``"money_market"``,
    ...) and is ``None`` for an UNCLASSIFIED row -- which means "nobody has
    said", never "ordinary share": a forty-year ledger is NULL-kind throughout
    and every other field of these payloads reads exactly as it did before.

    ``option`` is ``None`` unless ``kind == 'option'``; for a contract it is the
    stored terms, every value a STRING or None. ``multiplier`` and ``strike``
    are exact decimal strings rather than floats for the same reason money is
    (0.1 has no binary form), and a ``None`` multiplier means the terms were
    never recorded, NOT that it is 1 or 100 -- the model must not guess it."""
    kind = investments.security_kind(conn, symbol)
    terms = investments.option_terms(conn, symbol)
    opt = None
    if terms is not None:
        opt = {
            "multiplier": None if terms["multiplier"] is None else str(terms["multiplier"]),
            "underlying": terms["underlying"],
            "expiration": terms["expiration"],
            "strike": None if terms["strike"] is None else str(terms["strike"]),
            "right": terms["right"],
        }
    return {"kind": kind, "option": opt}


def holdings(conn, account, as_of: Optional[str] = None) -> dict:
    """An investment account's positions valued at market: quantity, cost
    basis, price, market value and gain per holding, plus cash and total.

    Each holding also carries `kind` (what the instrument IS -- null when the
    security is unclassified) and, for `kind` "option", an `option` object with
    the contract terms: `multiplier`, `underlying`, `expiration`, `strike` and
    `right` ("C"/"P"), all strings. An option's `quantity` is CONTRACTS and its
    `price` is the per-share premium, so `market_value` is contracts x premium x
    multiplier -- a short (negative) contract count values negative, as the
    liability it is."""
    aid = resolve_account(conn, account)
    d = _date(as_of, "as_of") if as_of else None
    v = investments.account_valuation(conn, aid, d)
    acct = ledger.get_account(conn, aid)
    return {"account": acct["name"] if acct else str(aid), "as_of": d or "latest",
            "cash": dollars(v.cash), "securities": dollars(v.securities),
            "total": dollars(v.total),
            "holdings": [{"symbol": h.symbol, "quantity": str(h.quantity),
                          "cost_basis": dollars(h.cost_basis),
                          "price": None if h.price is None else str(h.price),
                          "market_value": dollars(h.market_value),
                          "gain": dollars(h.gain),
                          **_instrument(conn, h.symbol)} for h in v.holdings],
            "unpriced": list(v.unpriced)}


def crypto_holdings(conn, account, as_of: Optional[str] = None) -> dict:
    """A crypto account's coin positions valued at market: quantity, cost basis,
    price, market value and gain per coin, plus the internal cash sleeve and
    total. Quantities and per-coin prices are exact decimal strings (wei-scale),
    money is decimal dollars; a coin with no recorded quote is listed under
    ``unpriced`` and valued at 0 rather than guessed at. The wallet address (kept
    in the same protected column as an account number) is never returned."""
    aid = resolve_account(conn, account)
    d = _date(as_of, "as_of") if as_of else None
    v = crypto.account_valuation(conn, aid, d)
    acct = ledger.get_account(conn, aid)
    return {"account": acct["name"] if acct else str(aid), "as_of": d or "latest",
            "cash": dollars(v.cash), "securities": dollars(v.securities),
            "total": dollars(v.total),
            "holdings": [{"symbol": h.symbol, "quantity": str(h.quantity),
                          "cost_basis": dollars(h.cost_basis),
                          "price": None if h.price is None else str(h.price),
                          "market_value": dollars(h.market_value),
                          "gain": dollars(h.gain)} for h in v.holdings],
            "unpriced": list(v.unpriced)}


def upcoming(conn, days: int = 30) -> dict:
    """Scheduled payments (bills, subscriptions, loan payments) due within
    ``days`` of today, from the app's own scheduled definitions."""
    today = _dt.date.today()
    horizon = (today + _dt.timedelta(days=int(days))).isoformat()
    rows = []
    for d in scheduled.list_scheduled(conn, active_only=True) + \
            scheduled.list_loan_schedules(conn, on_or_after=today.isoformat()):
        nd = d.get("next_date")
        if nd and nd <= horizon:
            rows.append({"source": d["source"], "payee": d["payee"],
                         "account": d["account_name"], "amount": dollars(d["amount"]),
                         "frequency": d["frequency"], "next_date": nd,
                         "category": d.get("category_label") or ""})
    rows.sort(key=lambda r: (r["next_date"], r["payee"]))
    return {"today": today.isoformat(), "through": horizon, "rows": rows,
            "total": dollars(sum(cents_of(r["amount"]) or 0 for r in rows))}


def loan(conn, account, upcoming_payments: int = 3) -> dict:
    """A loan account's terms, remaining principal, next payments and payoff
    date, from its amortization schedule."""
    aid = resolve_account(conn, account)
    lp = loans.get_loan_params(conn, aid)
    if lp is None:
        raise ValueError("that account has no loan parameters (set them in Loan Setup)")
    sched = loans.amortization_schedule(conn, aid)
    today = _today()
    future = [r for r in sched if r.date >= today]
    remaining = (future[0].balance + future[0].principal) if future else 0
    acct = ledger.get_account(conn, aid)
    rate = lp.rates[-1].annual_rate if lp.rates else None
    funder = loans.funding_account(conn, aid)
    funder_acct = ledger.get_account(conn, funder) if funder is not None else None
    return {"account": acct["name"] if acct else str(aid),
            "paid_from": funder_acct["name"] if funder_acct else None,
            "original_principal": dollars(lp.original_principal),
            "origination_date": lp.origination_date, "term_months": lp.term_months,
            "interval": lp.interval, "payment": dollars(lp.payment_amount),
            "annual_rate": None if rate is None else str(rate),
            "remaining_principal": dollars(remaining),
            "ledger_balance": dollars(ledger.account_balance(conn, aid)),
            "payoff_date": sched[-1].date if sched else None,
            "payments_remaining": len(future),
            "interest_remaining": dollars(sum(r.interest for r in future)),
            "next_payments": [{"date": r.date, "payment": dollars(r.payment),
                               "principal": dollars(r.principal),
                               "interest": dollars(r.interest), "escrow": dollars(r.escrow),
                               "balance_after": dollars(r.balance)}
                              for r in future[:max(0, int(upcoming_payments))]]}


# ---------------------------------------------------------------------------
# the retirement plan (SRD 5.8o): its inputs, its projection, what it sustains
# ---------------------------------------------------------------------------
# The projection is the Retirement Planner page's own engine
# (mammon/retirement_plan.py), built with no widget, so a model reads the same
# years the page draws. What-if parameters re-plan in memory and write nothing;
# the connection is query_only regardless. Names, relationships and birth
# month/year cross; the health columns on `people` never do (retirement.
# HEALTH_COLUMNS, denied below and in the SQL tool alike).

def _pct(value) -> Optional[str]:
    return None if value is None else str(value)


def _plan_people(conn) -> list:
    """Everyone on file, their plain columns only - never a health column."""
    out = []
    for p in retirement.list_people(conn):
        out.append({k: p[k] for k in retirement.PEOPLE_PLAIN_COLUMNS
                    if k in p.keys() and k not in retirement.HEALTH_COLUMNS})
    return out


def _months_text(months) -> Optional[str]:
    if months is None:
        return None
    years, rest = divmod(int(months), 12)
    return f"{years}" if not rest else f"{years} and {rest} months"


def _bracket_arg(ira_bracket) -> Optional[str]:
    if ira_bracket in (None, ""):
        return None
    rate = str(ira_bracket).strip().rstrip("%")
    if retirement.bracket_top_cents(rate, "single") is None:
        rates = [str(b.rate_percent) for b in retirement.tax_brackets("single")][:-1]
        raise ValueError(f"ira_bracket must be a bracket with a top, one of {rates}; "
                         f"got {ira_bracket!r}")
    return rate


def _terminal_age_arg(terminal_age) -> Optional[int]:
    if terminal_age is None:
        return None
    from mammon import retirement_plan as rplan
    age = int(terminal_age)
    if not rplan.MIN_TERMINAL_AGE <= age <= rplan.MAX_TERMINAL_AGE:
        raise ValueError(f"terminal_age must be {rplan.MIN_TERMINAL_AGE}.."
                         f"{rplan.MAX_TERMINAL_AGE}, got {age}")
    return age


def retirement_plan(conn) -> dict:
    """The retirement plan's INPUTS: the household (names, relationships,
    birth year/month, Social Security claim age and the monthly benefit it
    pays in today's dollars), the accounts the plan spends from with today's
    balances, the other income sources, the stored spending plan, the Roth
    conversions on file, and every setting the projection runs on. No
    projection here -- see `retirement_projection`."""
    from mammon import retirement_plan as rplan
    today = _dt.date.today()
    people = _plan_people(conn)
    planning = rplan.planning_people(conn)
    records = rplan.benefit_records(conn, planning) if planning else {}
    names = {int(p["id"]): p.get("name") for p in people}
    out_people = []
    for p in people:
        rec = records.get(int(p["id"]))
        born = p.get("birth_year")
        claim = rplan.claim_age_months(p) if born else None
        out_people.append({
            "id": int(p["id"]), "name": p.get("name"),
            "relationship": p.get("relationship"),
            "birth_year": born, "birth_month": p.get("birth_month"),
            "age_this_year": (today.year - int(born)) if born else None,
            "planned_claim_age": _months_text(claim),
            "full_retirement_age": (_months_text(retirement.full_retirement_age_months(int(born)))
                                    if born else None),
            "monthly_benefit_at_claim": dollars(rec.own_monthly_cents) if rec else None,
            "spousal_monthly_benefit": (dollars(rec.spousal_monthly_cents)
                                        if rec and rec.spousal_monthly_cents else None),
            "medicare_part_d": None if p.get("part_d") is None else bool(p.get("part_d")),
            "social_security_statement_date": p.get("ss_statement_date"),
        })
    exempt = retirement.spending_exempt_ids(conn)
    accounts = []
    for a in retirement.spending_accounts(conn):
        accounts.append({
            "id": int(a.account_id), "name": a.name,
            "treatment": "taxable" if a.is_taxable else a.treatment,
            "owner": names.get(a.owner_person_id) if a.owner_person_id else None,
            "balance": dollars(0 if a.planned else rplan.account_value_cents(conn, a.account_id)),
            "planned_roth_not_yet_opened": bool(a.planned),
            "current_employer_plan": bool(a.current_employer_plan),
            "is_ira": bool(a.is_ira),
            "after_tax_basis": dollars(a.after_tax_basis_cents) if a.after_tax_basis_cents else None,
            "inherited_death_year": a.inherited_death_year,
            "never_drawn_for_spending": int(a.account_id) in exempt,
        })
    sources = [{"name": s.name, "kind": s.kind, "amount_per_year": dollars(s.amount_cents),
                "start_year": s.start_year, "end_year": s.end_year,
                "change_pct_per_year": _pct(s.change_pct), "taxable": bool(s.taxable),
                "person": names.get(s.person_id) if s.person_id else None,
                "deferral_pct": _pct(s.deferral_pct) if s.kind == "salary" else None,
                "employer_match_pct": _pct(s.match_pct) if s.kind == "salary" else None,
                "qualified_dividends_or_ltcg": bool(s.preferential),
                "survivor_pct": _pct(s.survivor_pct) if s.kind == "pension" else None}
               for s in retirement.list_income_sources(conn)]
    params = retirement.get_withdrawal_plan(conn)
    scenario = retirement.get_survivor_scenario(conn)
    shortfall = retirement.get_ss_shortfall(conn)
    mix = retirement.get_plan_mix(conn)
    acct_names = {a["id"]: a["name"] for a in accounts}

    def acct_name(aid):
        aid = int(aid)
        if aid in acct_names:
            return acct_names[aid]
        row = ledger.get_account(conn, aid) if aid > 0 else None
        return row["name"] if row else f"planned Roth {aid}"

    return {
        "today": today.isoformat(),
        "plan_through_age": retirement.get_plan_through_age(conn),
        "filing_status": rplan.default_filing_status(conn),
        "people": out_people,
        "accounts": accounts,
        "income_sources": sources,
        "spending_plan": {
            "set": params.is_set,
            "yearly_spending": dollars(params.start_cents) if params.is_set else None,
            "increase_pct_per_year": _pct(params.increase_pct) if params.is_set else None,
            "start_year": params.start_year,
            "ira_draws_capped_at_bracket": params.bracket_rate,
        },
        "roth_conversions": [{"year": int(r["year"]), "from": acct_name(r["from_account_id"]),
                              "to": acct_name(r["to_account_id"]),
                              "amount": dollars(r["amount_cents"])}
                             for r in retirement.list_conversions(conn)],
        "settings": {
            "spending_order_after_minimums": list(retirement.get_spending_order(conn)),
            "tax_paid_from": retirement.get_tax_paid_from(conn),
            "bracket_index_pct": _pct(retirement.get_bracket_index_pct(conn)),
            "social_security_cola_pct": _pct(retirement.get_cola_pct(conn)),
            "social_security_shortfall": {"from_year": shortfall.year,
                                          "payable_pct": _pct(shortfall.payable_pct)},
            "state_tax_pct": _pct(retirement.get_state_tax_pct(conn)),
            "state_taxes_social_security": retirement.get_state_taxes_ss(conn),
            "state_excludes_retirement_income": retirement.get_state_excludes_retirement(conn),
            "medicare_growth_pct": _pct(retirement.get_medicare_growth_pct(conn)),
            "ssa44_appeal": retirement.get_ssa44_appeal(conn),
            "aca_benchmark_premium": dollars(retirement.get_aca_benchmark_cents(conn)),
            "defer_first_rmd_to_april": retirement.get_defer_first_rmd(conn),
            "community_property_state": retirement.get_community_property(conn),
            "end_of_plan_ira_tax_pct": _pct(retirement.get_end_ira_tax_pct(conn)),
            "projection_mix": (None if mix is None else
                               dict(zip(("stocks_pct", "bonds_pct", "cash_pct"),
                                        (str(v) for v in mix)))),
            "survivor_scenario": (None if scenario is None else {
                "deceased": names.get(scenario.deceased_id),
                "death_year": scenario.death_year,
                "survivor_spending_pct": _pct(scenario.spending_pct),
                "pension_survivor_pct": _pct(scenario.pension_pct)}),
        },
    }


def retirement_projection(conn, terminal_age: Optional[int] = None,
                          yearly_spending: Optional[str] = None,
                          increase_pct: Optional[str] = None,
                          start_year: Optional[int] = None,
                          ira_bracket: Optional[str] = None,
                          by_account: bool = False) -> dict:
    """The retirement plan year by year, as the Retirement Planner page
    projects it: each year's Social Security and other income, the spending
    the accounts must fund (plus the income tax and Medicare IRMAA / ACA cost
    that drawing it causes), what the pool can actually supply, the draws by
    account kind (IRA/401(k), taxable brokerage, Roth), the required minimum,
    the Roth conversions on file, and the invested pool entering each year
    (the median of the projection). `summary.runs_out_year` is the year the
    money runs out, or null if it lasts through `terminal_age` (default: the
    age stored in the planner).

    Two tax figures, both true: `income_tax` is the year's WHOLE income tax
    (federal, state, and the 10% on early withdrawals) -- the planner page's
    own figure: `summary.total_income_tax_in_todays_dollars` is the page's
    "Federal tax" plus "State tax" (its figures row is in today's dollars),
    and `total_income_tax` the same in each year's dollars.
    `summary.stored_withdrawals_out_of_date_from` is the first year whose
    stored withdrawals no longer match the plan computed from today's ledger
    (the planner flags this; re-applying the plan in Mammon updates them), or
    null. `tax_paid_from_accounts` is what the plan
    draws from the accounts to pay tax: before the plan's start year only the
    tax a Roth conversion adds (the paycheck pays the rest), the whole year's
    from then on.

    With no other arguments it projects the STORED spending plan. Giving
    `yearly_spending` (and optionally `increase_pct`, `start_year` -- the year
    spending starts, i.e. retirement -- and `ira_bracket`, e.g. "22", which
    caps IRA draws at that bracket's top and lets Roth pay the rest) runs a
    WHAT-IF instead: the same projection with those values, saved nowhere.
    Unspecified what-if values default to the stored plan's. `by_account`
    adds each account's draw and entering balance per year."""
    from mammon import retirement_plan as rplan
    age = _terminal_age_arg(terminal_age)
    bracket = _bracket_arg(ira_bracket)
    stored = retirement.get_withdrawal_plan(conn)
    what_if = any(v not in (None, "") for v in (yearly_spending, increase_pct,
                                                start_year, ira_bracket))
    with rplan.inflows_measured_once():
        planner = rplan.HouseholdPlanner(conn, terminal_age=age)
        years_shown = list(planner._years)
        people = rplan.planning_people(conn)
        accounts = planner.accounts()
        kind = {a.account_id: ("taxable" if a.is_taxable else a.treatment) for a in accounts}
        name = {a.account_id: a.name for a in accounts}
        out = {"today": planner._today.isoformat(), "terminal_age": planner.terminal_age,
               "filing_status": planner.filing_status,
               "basis": "what-if (nothing saved)" if what_if else "stored plan",
               "money": "nominal dollars of each year"}
        if not years_shown:
            out.update(summary=None, years=[],
                       note="nobody on file has a birth year, so there is no plan "
                            "horizon - add the household in the planner's Social "
                            "Security dialog")
            return out

        if what_if:
            spend = cents_of(yearly_spending) if yearly_spending not in (None, "") \
                else (stored.start_cents if stored.is_set else None)
            if not spend or spend <= 0:
                raise ValueError("a what-if needs yearly_spending (no spending plan is stored)")
            pct = Decimal(str(increase_pct)) if increase_pct not in (None, "") \
                else stored.increase_pct
            first = int(start_year) if start_year not in (None, "") else stored.start_year
            rate = bracket if ira_bracket not in (None, "") else stored.bracket_rate
        elif stored.is_set:
            spend, pct, first, rate = (stored.start_cents, stored.increase_pct,
                                       stored.start_year, stored.bracket_rate)
        else:
            spend = None

        born = min((int(p["birth_year"]) for p in rplan.horizon_people(conn)), default=None)
        ages_of = [(p.get("name") or p.get("relationship"), int(p["birth_year"]))
                   for p in people if p.get("birth_year")]

        def ages(year):
            return {n: year - b for n, b in ages_of}

        def conversions_in(year):
            return sum(int(f.conversion_out_cents)
                       for aid, f in planner.flows_for(year).items()
                       if kind.get(aid) == "deferred")

        def whole_year_taxes(bars, plan):
            # The page's own figure (retirement_plan.PlanTaxes): each year's
            # whole income tax, and its total nominal and in today's dollars.
            taxes = rplan.PlanTaxes(conn, bars, plan, planner.filing_status, planner)
            total, today_total = taxes.tax_totals(planner._today.year)
            return taxes.tax_by_year(), total, today_total

        if spend is None:
            # No household spending plan: the stored per-account withdrawals
            # (the required minimums the page seeds, and any typed by hand).
            rows = rplan.income_rows(conn, terminal_age=planner.terminal_age,
                                     today=planner._today)
            by_year, total, today_total = whole_year_taxes(rows, None)
            out["note"] = ("no household spending plan is stored, so this is the "
                           "per-account withdrawals on file (at least the required "
                           "minimums) - pass yearly_spending for a what-if")
            out["summary"] = {"total_income_tax": dollars(total),
                              "total_income_tax_in_todays_dollars": dollars(today_total)}
            out["years"] = [{"year": r.year, "ages": ages(r.year),
                             "social_security": dollars(r.social_security_cents),
                             "other_income": dollars(r.other_income_cents),
                             "ira_draws": dollars(r.deferred_draw_cents),
                             "taxable_account_draws": dollars(r.taxable_draw_cents),
                             "roth_draws": dollars(r.roth_draw_cents),
                             "roth_conversions": dollars(r.conversion_cents),
                             "income_tax": dollars(by_year.get(r.year, 0)),
                             "pool_value": dollars(r.fund_value_cents)}
                            for r in rows]
            return out

        plan = planner.plan_for(spend, pct, start_year=first, bracket_rate=rate)
        covered, _room = planner.household_income(planner.apply_years(), accounts, None)
        first_year = int(first) if first is not None else years_shown[0]
        scenario = retirement.get_survivor_scenario(conn)
        shown = set(years_shown)
        if what_if:
            # The bars a what-if would draw, from its own plan: the stored ones
            # are the stored plan's withdrawals.
            sources = retirement.plan_income_sources(conn)
            bars = []
            for e in plan.years:
                if e.year not in shown:
                    continue
                drawn = {"deferred": 0, "roth": 0, "taxable": 0}
                for aid, cents in e.amounts.items():
                    drawn[kind.get(int(aid), "taxable")] += int(cents)
                bars.append(rplan.IncomeYear(
                    year=e.year, age=(e.year - born) if born is not None else None,
                    social_security_cents=rplan.social_security_cents(
                        conn, e.year, people, base_year=planner._today.year),
                    deferred_draw_cents=drawn["deferred"], roth_draw_cents=drawn["roth"],
                    conversion_cents=conversions_in(e.year),
                    fund_value_cents=int(e.pool_cents),
                    other_income_cents=sum(src.cents_in(e.year) for src in sources),
                    taxable_draw_cents=drawn["taxable"]))
        else:
            bars = rplan.income_rows(conn, terminal_age=planner.terminal_age,
                                     today=planner._today)
        whole_tax, whole_total, whole_today = whole_year_taxes(bars, plan)
        rows = []
        for e in plan.years:
            if e.year not in shown:
                continue
            ss = rplan.social_security_cents(conn, e.year, people,
                                             base_year=planner._today.year)
            spending = 0
            if e.year >= first_year:
                spending = retirement.household_amount_cents(spend, pct, e.year - first_year)
                if scenario is not None and scenario.widowed(e.year):
                    spending = int((Decimal(spending) * scenario.spending_pct / 100)
                                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))
            by_kind = {"deferred": 0, "roth": 0, "taxable": 0}
            for aid, cents in e.amounts.items():
                by_kind[kind.get(int(aid), "taxable")] += int(cents)
            row = {"year": e.year, "ages": ages(e.year),
                   "spending": dollars(spending),
                   "social_security": dollars(ss),
                   "other_income": dollars(covered.get(e.year, 0) - ss),
                   "needed_from_accounts": dollars(e.target_cents),
                   "drawn": dollars(e.drawn_cents),
                   "shortfall": dollars(e.shortfall_cents),
                   "required_minimum": dollars(e.floor_cents),
                   "ira_draws": dollars(by_kind["deferred"]),
                   "taxable_account_draws": dollars(by_kind["taxable"]),
                   "roth_draws": dollars(by_kind["roth"]),
                   "roth_conversions": dollars(conversions_in(e.year)),
                   "income_tax": dollars(whole_tax.get(e.year, 0)),
                   "tax_paid_from_accounts": dollars(e.tax_cents),
                   "early_withdrawal_penalty": dollars(e.penalty_cents),
                   "irmaa_and_aca_cost": dollars(e.surcharge_cents),
                   "realized_capital_gains": dollars(e.gains_cents),
                   "pool_value_entering_year": dollars(e.pool_cents)}
            if by_account:
                row["accounts"] = {name.get(int(aid), str(aid)): {
                    "draw": dollars(e.amounts.get(aid, 0)),
                    "balance_entering_year": dollars(e.balances.get(aid, 0))}
                    for aid in sorted(set(e.amounts) | set(e.balances))}
            rows.append(row)
        last = years_shown[-1]
        runs_out = plan.depleted_year if (plan.depleted_year is not None
                                          and plan.depleted_year <= last) else None
        out["parameters"] = {"yearly_spending": dollars(spend),
                             "increase_pct_per_year": _pct(pct),
                             "start_year": first_year,
                             "ira_draws_capped_at_bracket": rate}
        out["summary"] = {
            "first_year": years_shown[0], "last_year": last,
            "lasts_through_last_year": runs_out is None,
            "runs_out_year": runs_out,
            "runs_out_age": (runs_out - born) if (runs_out is not None and born) else None,
            "ending_pool_value": dollars(planner.ending_balance_cents(plan, last)),
            "total_income_tax": dollars(whole_total),
            "total_income_tax_in_todays_dollars": dollars(whole_today),
            "total_tax_paid_from_accounts": dollars(sum(int(r.tax_cents) for r in plan.years
                                                        if r.year in shown)),
            "total_irmaa_and_aca_cost": dollars(sum(int(r.surcharge_cents) for r in plan.years
                                                    if r.year in shown)),
            "years_where_required_minimums_exceed_spending":
                [y for y in plan.floor_raised_years if y in shown],
        }
        if not what_if:
            # The stored withdrawals (what the page's bars and tax read) as
            # against the plan computed from today's ledger: when the data
            # moved since the last Apply, the page flags its schedule.
            stale = rplan.stale_withdrawal_years(conn, plan)
            out["summary"]["stored_withdrawals_out_of_date_from"] = \
                stale[0] if stale else None
        out["years"] = rows
        return out


def retirement_max_spending(conn, increase_pct: Optional[str] = None,
                            start_year: Optional[int] = None,
                            ira_bracket: Optional[str] = None,
                            reserve: Optional[str] = None,
                            terminal_age: Optional[int] = None) -> dict:
    """The largest starting yearly spending the retirement plan sustains
    through `terminal_age` (default: the age stored in the planner) and still
    leaves `reserve` dollars in the pool at the end -- the planner's "most we
    can spend" search, to the nearest $1,000. `increase_pct`, `start_year` and
    `ira_bracket` default to the stored spending plan's. Nothing is saved.
    A computed figure, not advice: it says what the projection sustains."""
    from mammon import retirement_plan as rplan
    age = _terminal_age_arg(terminal_age)
    bracket = _bracket_arg(ira_bracket)
    stored = retirement.get_withdrawal_plan(conn)
    pct = Decimal(str(increase_pct)) if increase_pct not in (None, "") else stored.increase_pct
    first = int(start_year) if start_year not in (None, "") else stored.start_year
    rate = bracket if ira_bracket not in (None, "") else stored.bracket_rate
    keep = cents_of(reserve) or 0
    with rplan.inflows_measured_once():
        planner = rplan.HouseholdPlanner(conn, terminal_age=age)
        if not planner._years:
            raise ValueError("nobody on file has a birth year, so there is no plan horizon")
        most = planner.most_we_can_spend(pct, start_year=first, bracket_rate=rate,
                                         reserve_cents=keep)
    return {"yearly_spending": dollars(most),
            "increase_pct_per_year": _pct(pct),
            "start_year": first if first is not None else planner._years[0],
            "ira_draws_capped_at_bracket": rate,
            "reserve_at_end": dollars(keep),
            "through_year": planner._years[-1], "terminal_age": planner.terminal_age}


# ---------------------------------------------------------------------------
# the long tail: schema + read-only SQL
# ---------------------------------------------------------------------------
def schema(conn) -> dict:
    """Tables and columns the SQL tool can read (identifying columns omitted).
    Money columns are integer cents; dates ISO text; quantities/prices text."""
    tables = {}
    for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall():
        cols = []
        for c in conn.execute(f"PRAGMA table_info({r['name']})").fetchall():
            if (r["name"], c["name"]) in SENSITIVE_COLUMNS:
                continue
            cols.append({"name": c["name"], "type": c["type"] or ""})
        tables[r["name"]] = cols
    return {"tables": tables,
            "notes": ["amounts are integer cents (negative = money out)",
                      "a transfer is a transaction with transfer_account_id set; "
                      "its mirror in the other account carries the opposite amount",
                      "a split transaction's category is on its splits rows",
                      "scheduled = 1 rows are pre-entered placeholders, not posted"]}


_SQL_ALLOWED = re.compile(r"^\s*(select|with)\b", re.I)


def _authorizer(action, arg1, arg2, dbname, source):
    # Reads are fine except for identifying columns, which read as NULL; every
    # other operation is denied outright.
    if action == sqlite3.SQLITE_READ:
        if (arg1, arg2) in SENSITIVE_COLUMNS:
            return sqlite3.SQLITE_IGNORE
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def query(conn, sql: str, limit: Optional[int] = DEFAULT_LIMIT) -> dict:
    """Run one read-only SELECT (see ``schema`` for tables). Anything that
    writes is refused; identifying columns come back NULL; rows are capped."""
    text = (sql or "").strip().rstrip(";").strip()
    if not _SQL_ALLOWED.match(text):
        raise ValueError("only a single SELECT (or WITH ... SELECT) is allowed")
    if ";" in text:
        raise ValueError("one statement at a time")
    n = _limit(limit)
    conn.set_authorizer(_authorizer)
    try:
        cur = conn.execute(text)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(n + 1)
    finally:
        conn.set_authorizer(None)
    truncated = len(rows) > n
    return {"columns": cols, "count": min(len(rows), n), "truncated": truncated,
            "rows": [list(r) for r in rows[:n]]}


def lots(conn, account, symbol: Optional[str] = None, as_of: Optional[str] = None) -> dict:
    """Open tax lots in an investment account (optionally one symbol): when
    each was acquired, shares, cost, price, market value, gain, and whether
    selling it now would be a long- or short-term gain. Also the account's
    cost-basis method (average, fifo or lifo).

    Each lot carries `kind` and, for an option, the same `option` terms object
    `holdings` returns; an option lot's `quantity` is CONTRACTS."""
    aid = resolve_account(conn, account)
    d = _date(as_of, "as_of") if as_of else None
    acct = ledger.get_account(conn, aid)
    return {"account": acct["name"] if acct else str(aid), "as_of": d or "today",
            "method": investments.get_lot_method(conn, aid),
            "lots": [{"symbol": l.symbol, "acquired": l.acquired,
                      "quantity": str(l.quantity), "cost": dollars(l.cost),
                      "price": None if l.price is None else str(l.price),
                      "market_value": dollars(l.market_value), "gain": dollars(l.gain),
                      "term": l.term, "days_held": l.days_held,
                      **_instrument(conn, l.symbol)}
                     for l in portfolio.open_lots(conn, aid, symbol=symbol, as_of=d)]}


def capital_gains(conn, account, start: Optional[str] = None,
                  end: Optional[str] = None, symbol: Optional[str] = None) -> dict:
    """Realized gains and losses from sales in an investment account with
    sale dates in a range (a tax year, say), one row per lot sold -- acquired
    and sold dates, shares, proceeds, basis, gain, short/long term -- with
    the Schedule D totals by term."""
    aid = resolve_account(conn, account)
    s = _date(start, "start") if start else None
    e = _date(end, "end") if end else None
    acct = ledger.get_account(conn, aid)
    gains = portfolio.capital_gains(conn, aid, s, e, symbol)
    summary = {k: {"proceeds": dollars(v["proceeds"]), "basis": dollars(v["basis"]),
                   "gain": dollars(v["gain"]), "lots": v["lots"]}
               for k, v in portfolio.gains_summary(gains).items()}
    return {"account": acct["name"] if acct else str(aid), "start": s, "end": e,
            "method": investments.get_lot_method(conn, aid),
            "rows": [{"symbol": g.symbol, "acquired": g.acquired, "sold": g.sold,
                      "quantity": str(g.quantity), "proceeds": dollars(g.proceeds),
                      "basis": dollars(g.basis), "gain": dollars(g.gain), "term": g.term}
                     for g in gains],
            "summary": summary}


def performance(conn, account, start: str, end: str, symbol: Optional[str] = None) -> dict:
    """Money-weighted return (IRR) of an investment account -- or of one
    security in it -- over a date range: starting and ending value, money
    moved in and out, income received, the gain, and the annualized rate as
    a percentage."""
    aid = resolve_account(conn, account)
    s, e = _date(start, "start"), _date(end, "end")
    p = (portfolio.security_performance(conn, aid, symbol, s, e) if symbol
         else portfolio.account_performance(conn, aid, s, e))
    acct = ledger.get_account(conn, aid)
    return {"account": acct["name"] if acct else str(aid), "symbol": symbol,
            "start": s, "end": e, "start_value": dollars(p.start_value),
            "end_value": dollars(p.end_value), "money_in": dollars(p.money_in),
            "money_out": dollars(p.money_out), "income": dollars(p.income),
            "gain": dollars(p.gain),
            "irr_pct": None if p.irr is None else round(p.irr * 100.0, 2)}


def allocation(conn, accounts=None, as_of: Optional[str] = None,
               scope: str = "investments") -> dict:
    """Asset allocation: market value and share by asset class, by security and
    by account, plus the symbols left out for want of a price.

    ``accounts`` names the accounts outright. Otherwise ``scope`` chooses them:
    `investments` (the investment accounts, Quicken's answer), `with_cash`
    (those plus checking/savings/cash) or `everything` (those plus property and
    other asset accounts, each counted under the asset class recorded for it).
    Debt is never part of an allocation.

    OPTION CONTRACTS are excluded from every number here -- the total, the
    classes, the securities and the account slices -- because a contract is not
    shares of its underlying: counting one as 100 shares would invent exposure
    the premium never bought. They are not dropped silently either: each is
    named in `excluded_options` with its premium value in dollars, and `note` is
    the sentence to show alongside the percentages. Use `holdings` for what the
    contracts themselves are worth."""
    ids = resolve_accounts(conn, accounts)
    d = _date(as_of, "as_of") if as_of else None
    if scope not in portfolio.ALLOCATION_SCOPES:
        raise ValueError(f"unknown scope {scope!r}; one of "
                         f"{', '.join(portfolio.ALLOCATION_SCOPES)}")
    a = portfolio.allocation(conn, ids, d, scope=scope)

    def slices(rows):
        return [{"key": s.key, "label": s.label, "value": dollars(s.value),
                 "pct": round(s.pct, 2)} for s in rows]

    return {"as_of": d or "latest", "scope": a.scope, "total": dollars(a.total),
            "by_class": slices(a.by_class), "by_security": slices(a.by_security),
            "by_account": slices(a.by_account), "unpriced": a.unpriced,
            "excluded_options": [{"symbol": sym, "market_value": dollars(v)}
                                 for sym, v in a.excluded_options],
            "excluded_options_value": dollars(a.option_value),
            "note": a.options_note}


def investment_performance(conn, accounts=None, as_of: Optional[str] = None) -> dict:
    """Consolidated per-holding investment performance across the investment
    accounts (or just those named in `accounts`): for every security ever held,
    its shares, average and current price, cost basis, market value, unrealized
    gain and its percent return, realized gain, dividend/interest income and
    return of capital -- plus the portfolio totals. Positions are valued at prices
    as of `as_of` (default: the latest the ledger knows); shares and cost are the
    current holding, not a rewind to that date. This is the snapshot Holdings-style
    view; use `performance` for a money-weighted (IRR) return over a range."""
    ids = resolve_accounts(conn, accounts)
    d = _date(as_of, "as_of") if as_of else None
    rep = _invperf(conn, d, account_ids=ids)

    def pct(value):
        return None if value is None else round(float(value), 2)

    return {
        "as_of": d or "latest",
        "holdings": [{"account": h.account_name, "symbol": h.symbol,
                      "shares": str(h.quantity), "open": h.is_open,
                      "avg_cost": None if h.avg_cost is None else str(h.avg_cost),
                      "price": None if h.price is None else str(h.price),
                      "cost_basis": dollars(h.cost_basis),
                      "market_value": dollars(h.market_value),
                      "unrealized_gain": dollars(h.unrealized_pl),
                      "pct_return": pct(h.pct_return),
                      "realized_gain": dollars(h.realized_pl),
                      "dividends": dollars(h.dividends),
                      "return_of_capital": dollars(h.return_of_capital),
                      **_instrument(conn, h.symbol)}
                     for h in rep.holdings],
        "totals": {"cost_basis": dollars(rep.total_cost_basis),
                   "market_value": dollars(rep.total_market_value),
                   "unrealized_gain": dollars(rep.total_unrealized_pl),
                   "realized_gain": dollars(rep.total_realized_pl),
                   "dividends": dollars(rep.total_dividends),
                   "return_of_capital": dollars(rep.total_return_of_capital),
                   "pct_return": pct(rep.pct_return)},
    }


def security_mixtures(conn) -> dict:
    """Which securities are counted as SEVERAL asset classes rather than one.

    A fund is not a single class: a target-date fund is roughly 58% equity, 40%
    bonds and 2% cash, and `allocation` splits its value accordingly wherever a
    mixture is recorded. Securities without one count wholly under their single
    asset class. `as_of` is when the split was last looked up -- a fund's
    composition drifts with its own holdings, so an old split is stale rather
    than wrong."""
    out = []
    for symbol, weights in sorted(security_mix.all_mixtures(conn).items()):
        meta = security_mix.mixture_meta(conn, symbol) or {}
        out.append({
            "symbol": symbol,
            "source": meta.get("source"),
            "as_of": meta.get("as_of"),
            "weights": [{"asset_class": cls,
                         "label": portfolio.ASSET_CLASS_LABELS.get(cls, cls),
                         "pct": float(pct)}
                        for cls, pct in sorted(weights.items(),
                                               key=lambda kv: (-kv[1], kv[0]))],
        })
    return {"securities": out, "count": len(out)}


def allocation_drift(conn, target: Optional[str] = None,
                     as_of: Optional[str] = None) -> dict:
    """How far the real asset mix has drifted from a TARGET mix.

    ``target`` names a target (its id or name); omitted, the active one is used.
    Percentages are of the target's SLEEVE -- the accounts it governs, normally
    the investment accounts. Property is reported separately under `fixed`,
    because nothing rebalances by selling part of a house.

    Each class carries its target and current share, the signed gap in
    percentage points and relative to its own weight, `move` -- the dollars that
    would return it to target, positive to buy -- and `action`, the verb for that
    move. CASH is never `sell`: it cannot be traded to its own weight, so an
    overweight cash line is `invest` (deploy the surplus into underweight
    securities) and an underweight one is `raise` (sell overweight securities to
    top it up). `out_of_band` marks the classes past the target's rebalancing
    bands (by default the 5/25 rule: 5 percentage points, or 25% of the class's
    own weight, whichever is tighter). Nothing here trades; the figures are
    arithmetic, and a sale in a taxable account would realize a gain this does
    not model."""
    tid = None
    if target is not None:
        rows = rebalance.list_targets(conn)
        text = str(target).strip().lower()
        match = [r for r in rows
                 if str(r["id"]) == text or (r["name"] or "").strip().lower() == text]
        if not match:
            names = ", ".join(sorted((r["name"] or "") for r in rows)) or "none defined"
            raise ValueError(f"unknown target {target!r}; targets: {names}")
        tid = int(match[0]["id"])
    d = _date(as_of, "as_of") if as_of else None
    r = rebalance.drift(conn, tid, as_of=d)
    return {
        "target": r.target_name,
        "as_of": d or "latest",
        "sleeve": r.sleeve,
        "sleeve_total": dollars(r.sleeve_total),
        "target_totals_100": r.target_is_complete,
        "target_total_pct": float(r.target_total_pct),
        "band_abs_pct": float(r.band_abs_pct),
        "band_rel_pct": float(r.band_rel_pct),
        "needs_rebalance": r.needs_rebalance,
        "to_move": dollars(r.to_move_cents),
        "classes": [{
            "asset_class": row.asset_class,
            "label": row.label,
            "target_pct": float(row.target_pct),
            "current_pct": round(float(row.current_pct), 2),
            "drift_pct": round(float(row.drift_pct), 2),
            "drift_rel_pct": (None if row.drift_rel_pct is None
                              else round(float(row.drift_rel_pct), 2)),
            "current": dollars(row.current_cents),
            "target": dollars(row.target_cents),
            "move": dollars(row.move_cents),
            "action": row.action,
            "out_of_band": row.out_of_band,
        } for row in r.rows],
        "fixed": [{"label": label, "value": dollars(cents)}
                  for label, cents in r.fixed_rows],
        "fixed_total": dollars(r.fixed_total),
        "summary": rebalance.describe(r),
    }


# ---------------------------------------------------------------------------
# budgets (roadmap item 6): plan vs actual, read-only
# ---------------------------------------------------------------------------
def list_budgets(conn, include_inactive: bool = True) -> dict:
    """Every budget (a named spending plan) with its id, name and active flag.
    Pass a budget's id to `budget_vs_actual` or `budget_ytd`."""
    return {"budgets": [{"id": b.id, "name": b.name, "active": b.active}
                        for b in budgets.list_budgets(
                            conn, include_inactive=include_inactive)]}


def _budget_report(rep) -> dict:
    """A :class:`mammon.reports.budget.BudgetRangeReport` as a JSON dict, money
    in decimal dollars. ``actual`` is money out as a positive magnitude and
    ``remaining`` is budgeted minus actual (negative = overspent)."""
    return {
        "budget_id": rep.budget_id,
        "budget": rep.budget_name,
        "periods": rep.periods,
        # A budget GROUP (a named pot over several categories) is one row with
        # `group: true` and its group id; it has no category id of its own.
        "categories": [{"category": r.category_name,
                        "category_id": None if r.is_group else r.category_id,
                        "group": bool(r.is_group),
                        "group_id": r.group_id if r.is_group else None,
                        "budgeted": dollars(r.budgeted_cents),
                        "actual": dollars(r.actual_cents),
                        "remaining": dollars(r.remaining_cents)}
                       for r in rep.category_totals],
        "by_period": [{"period": t.period, "budgeted": dollars(t.budgeted_cents),
                       "actual": dollars(t.actual_cents),
                       "remaining": dollars(t.remaining_cents)}
                      for t in rep.period_totals],
        "budgeted": dollars(rep.total_budgeted_cents),
        "actual": dollars(rep.total_actual_cents),
        "remaining": dollars(rep.total_remaining_cents),
    }


def budget_vs_actual(conn, budget_id: int, start: str, end: Optional[str] = None,
                     include_unbudgeted: bool = True) -> dict:
    """Budgeted vs actual per category over a month or a range of months.

    `start` and `end` are ISO YYYY-MM months; omit `end` for a single month.
    Returns per-category totals summed over the range, each month's own totals
    under `by_period`, and grand totals. `include_unbudgeted` also counts a
    category spent with no line that month (budgeted 0). Transfers excluded."""
    rep = _budget.budget_vs_actual_range(
        conn, budget_id, start, end or start,
        include_unbudgeted=include_unbudgeted)
    return _budget_report(rep)


def budget_ytd(conn, budget_id: int, year: int, through_month: int = 12,
               include_unbudgeted: bool = True) -> dict:
    """Year-to-date budgeted vs actual: January through `through_month` (1..12,
    default December) of `year`. Same shape as `budget_vs_actual`."""
    rep = _budget.budget_vs_actual_ytd(
        conn, budget_id, year, through_month=through_month,
        include_unbudgeted=include_unbudgeted)
    return _budget_report(rep)


# ---------------------------------------------------------------------------
# registry (what the server binds; what the tests iterate)
# ---------------------------------------------------------------------------
TOOLS: dict[str, Callable[..., dict]] = {
    "overview": overview,
    "list_accounts": list_accounts,
    "list_categories": list_categories,
    "net_worth": net_worth,
    "account_balances": account_balances,
    "balances_over_time": balances_over_time,
    "income_expense": income_expense,
    "cash_flow": cash_flow,
    "compare_periods": compare_periods,
    "category_averages": category_averages,
    "spending_by_category": spending_by_category,
    "by_payee": by_payee,
    "by_tag": by_tag,
    "transactions": transactions,
    "search": search,
    "holdings": holdings,
    "crypto_holdings": crypto_holdings,
    "lots": lots,
    "capital_gains": capital_gains,
    "performance": performance,
    "investment_performance": investment_performance,
    "allocation": allocation,
    "allocation_drift": allocation_drift,
    "security_mixtures": security_mixtures,
    "upcoming": upcoming,
    "loan": loan,
    "list_budgets": list_budgets,
    "budget_vs_actual": budget_vs_actual,
    "budget_ytd": budget_ytd,
    "retirement_plan": retirement_plan,
    "retirement_projection": retirement_projection,
    "retirement_max_spending": retirement_max_spending,
    "schema": schema,
    "query": query,
}

INSTRUCTIONS = (
    "Mammon is the user's personal-finance ledger. Every tool is READ-ONLY. "
    "Start with `overview` to learn today's date, the ledger's date span and the "
    "account names. Amounts are decimal dollar strings, negative = money out; "
    "dates are ISO YYYY-MM-DD and ranges are inclusive. Prefer the report tools "
    "(income_expense, cash_flow, spending_by_category, by_payee, compare_periods, "
    "category_averages, balances_over_time) over listing rows; use "
    "`transactions` or `search` for specific rows and `query` (with `schema`) for "
    "anything else. For investments: `holdings` (positions at market), `lots` "
    "(open tax lots), `capital_gains` (realized gains by term for a date range), "
    "`performance` (money-weighted return, IRR), `investment_performance` (a "
    "consolidated per-holding snapshot: cost basis, market value, gain, % return, "
    "income) and `allocation` (by asset class, over investments alone or "
    "everything owned -- see its `scope`) and `allocation_drift` (how far that "
    "mix has strayed from a target, and what would return it). "
    "Each position carries `kind` and, for an option, an `option` object of "
    "contract terms as strings; an option's quantity is CONTRACTS and its market "
    "value is contracts x premium x multiplier, negative when written. "
    "`allocation` leaves option contracts out of every number and names them in "
    "`excluded_options`, with a `note` to show the user. "
    "For budgets: `list_budgets`, then `budget_vs_actual` (a month or "
    "range) or `budget_ytd` (year-to-date). "
    "For retirement: `retirement_plan` (the household, accounts, income and "
    "settings the plan runs on), `retirement_projection` (year by year: income, "
    "draws, tax, IRMAA, the pool, the year the money runs out; give "
    "yearly_spending etc. for a what-if that saves nothing) and "
    "`retirement_max_spending`. Report the plan's figures as computed results "
    "with their assumptions, not as advice. Account numbers and login details "
    "are never available."
)
