"""Generic CSV parser with loose column mapping (e.g. Fidelity, bank exports).

CSV has no standard, so we map by trying a list of likely header names per field
(the approach in legacy/fidelity_csv_to_qif.py). A row is treated as an
investment row when it carries a security symbol and a share quantity; otherwise
it is cash. Cash amount comes from a single ``Amount`` column, or from separate
``Debit``/``Credit`` (a.k.a. Withdrawal/Deposit) columns.

On top of that generic path sits a small **column-profile** layer for statement
exports whose columns and vocabulary differ enough that the generic guesser gets
them wrong (:data:`_PROFILES`): Fidelity NetBenefits 401k, State 529, and T. Rowe
Price. These are contribution/NAV-unit files -- a "fund" (often ticker-less, so
its NAME is the holding key), a unit count, and a per-unit NAV, but NO per-lot
cost basis. Each profile knows that file's column names and translates its own
activity vocabulary ("Contribution", "Redemption", ...) into the canonical
Quicken actions the holdings replay understands: a contribution/purchase that
buys units -> ``BuyX`` (adds shares + cost from the dollar amount, cash nets to
zero), a reinvested dividend -> ``ReinvDiv``, a withdrawal/redemption that sells
units -> ``SellX``. The per-unit NAV rides along on the transaction's ``price``
and is captured into ``price_history`` by the shared import path (source
``qif-txn``) -- for a NAV fund with no quote series that IS its last-known value.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from mammon.importers import tabular
from mammon.importers.record import (
    NormalizedTxn,
    normalize_security_name,
    derive_investment_amounts,
    dollars_to_cents,
    normalize_investment_signs,
    normalize_payee,
    parse_date,
)
from mammon.importers.tabular import AMOUNT_COLS, DATE_COLS, PAYEE_COLS

# Status values that mean the transaction has settled (-> cleared=1).
_CLEARED_STATUS = {"posted", "cleared", "settled", "paid", "processed"}


@dataclass(frozen=True)
class _Profile:
    """A per-institution CSV column map for NAV-unit statement exports.

    ``signature`` is the set of lower-cased header names that must ALL be present
    for the file to be this profile (chosen to be disjoint across profiles and
    absent from the generic brokerage export). The ``*_cols`` tuples are tried in
    order via :func:`_col`. ``action_map`` translates the file's own lower-cased
    activity text into a canonical Quicken action; unmapped text passes through
    verbatim (so an unrecognized row still lands, just with no share effect)."""

    name: str
    signature: frozenset
    date_cols: tuple
    fund_cols: tuple
    action_cols: tuple
    amount_cols: tuple
    unit_cols: tuple
    price_cols: tuple
    action_map: dict = field(default_factory=dict)
    memo_cols: tuple = ()
    fitid_cols: tuple = ("Reference", "Confirmation Number", "Confirmation #", "Transaction ID")


# Contribution/purchase that buys units -> BuyX (adds shares + cost, cash zero).
# Reinvested dividend -> ReinvDiv. Withdrawal/redemption that sells units -> SellX.
#
# Resist adding a profile per institution. A profile is only warranted when a
# source needs COLUMN names the generic aliases below do not already cover; the
# generic investment path handles any statement whose header names a security,
# a share count, an amount and an activity column, whatever the broker is
# called. An Interactive Brokers profile lived here briefly and was deleted once
# the generic path was fixed to treat a file (not each row) as an investment
# source -- every column it listed (Date, Symbol, Transaction Type, Net Amount,
# Quantity, Description) was already a generic alias. Activity VOCABULARY is
# never a reason to add one: that is learned per install from the user's review
# corrections (rename_tree, kind="action").
_PROFILES: tuple = (
    _Profile(
        name="Fidelity NetBenefits 401k",
        # An "Investment" + "Transaction Type" pair is unique to this statement
        # export (a generic brokerage export names a "Symbol"/"Action", not an
        # "Investment"). Keying on those two rather than a specific share column
        # matches BOTH "Shares" and the real download's "Shares/Unit" header.
        signature=frozenset({"investment", "transaction type"}),
        date_cols=("Date", "Transaction Date"),
        fund_cols=("Investment", "Investment Name", "Fund"),
        action_cols=("Transaction Type", "Activity"),
        amount_cols=("Amount", "Dollar Amount", "Amount ($)"),
        unit_cols=("Shares", "Shares/Unit", "Units"),
        price_cols=("Share Price", "Price Per Share", "Unit Price"),
        action_map={
            "contribution": "BuyX",
            "contributions": "BuyX",
            "employee contribution": "BuyX",
            "employer match": "BuyX",
            "exchange in": "BuyX",
            "dividend": "ReinvDiv",
            "dividend reinvestment": "ReinvDiv",
            "exchange out": "SellX",
            "redemption": "SellX",
            # Plan fees are paid by REDEEMING units, so they remove shares
            # without being a sale -- exactly ShrsOut, which is what the same
            # plan's QIF export calls them. Unmapped, they landed as raw
            # activity text that the holdings replay does not understand, so
            # the shares were never actually removed.
            "recordkeeping fee": "ShrsOut",
            "administrative fees": "ShrsOut",
            "administrative fee": "ShrsOut",
            "plan administrative fee": "ShrsOut",
            "advisory fee": "ShrsOut",
            "management fee": "ShrsOut",
        },
        memo_cols=("Transaction Type", "Activity"),
    ),
    _Profile(
        name="State 529",
        signature=frozenset({"fund", "units", "unit price"}),
        date_cols=("Trade Date", "Date"),
        fund_cols=("Fund", "Fund Name", "Investment Option"),
        action_cols=("Transaction Type", "Activity"),
        amount_cols=("Dollar Amount", "Amount", "Amount ($)"),
        unit_cols=("Units", "Shares"),
        price_cols=("Unit Price", "Price Per Unit", "NAV"),
        action_map={
            "contribution": "BuyX",
            "dividend reinvestment": "ReinvDiv",
            "reinvested dividend": "ReinvDiv",
            "withdrawal": "SellX",
            "qualified withdrawal": "SellX",
        },
        memo_cols=("Transaction Type", "Activity"),
    ),
    _Profile(
        name="T. Rowe Price",
        signature=frozenset({"fund name", "transaction description"}),
        date_cols=("Trade Date", "Date"),
        fund_cols=("Fund Name", "Fund"),
        action_cols=("Transaction Description", "Transaction Type"),
        amount_cols=("Amount", "Dollar Amount", "Amount ($)"),
        unit_cols=("Shares", "Units"),
        price_cols=("Share Price", "Price Per Share", "Unit Price"),
        action_map={
            "purchase": "BuyX",
            "reinvest dividend": "ReinvDiv",
            "dividend reinvestment": "ReinvDiv",
            "redemption": "SellX",
        },
        memo_cols=("Transaction Description", "Transaction Type"),
    ),
)


def _try_date(value) -> Optional[str]:
    """Parse a date, returning ``None`` instead of raising on junk. Real exports
    trail a disclaimer/footer (e.g. Fidelity's "Date downloaded ..." and multi-line
    legal text) after the data; such rows land in the DictReader with unparseable
    date cells and must be skipped, not crash the whole import."""
    if not value:
        return None
    try:
        return parse_date(value)
    except ValueError:
        return None


def _detect_profile(fieldnames) -> Optional[_Profile]:
    headers = {(f or "").strip().lower() for f in (fieldnames or [])}
    for prof in _PROFILES:
        if prof.signature <= headers:
            return prof
    return None


# NOTE: activity text is NOT filtered here. A row the importer drops is a row the
# user cannot see, and therefore cannot correct or delete -- strictly worse than a
# row mapped to the wrong action, which at least shows up in the review list with
# an Action the user can edit. "Change in Market Value" was being skipped on the
# reasoning that a zero-unit valuation is not a transaction; that reasoning is
# probably right and was still the wrong call, because it made the decision on the
# user's behalf and silently. Such rows now reach review like everything else and
# are discarded there, which is what a user does in Quicken.


def _profile_txn(prof: _Profile, row: dict, default_account: Optional[str]) -> Optional[NormalizedTxn]:
    iso_date = _try_date(_col(row, *prof.date_cols))
    if iso_date is None:
        return None
    fund = normalize_security_name(_col(row, *prof.fund_cols))
    raw_action = _col(row, *prof.action_cols)
    action = prof.action_map.get(raw_action.strip().lower(), raw_action)
    account = _col(row, "Account", "Account Name", "Account Number") or (default_account or "")
    # Derive whichever of shares / per-unit price / dollar amount the file omits
    # (many NAV statements give units + dollars but no explicit per-unit price).
    quantity, price, amount_cents, _ok = derive_investment_amounts(
        _col(row, *prof.unit_cols),
        _col(row, *prof.price_cols),
        _col(row, *prof.amount_cols),
    )
    return NormalizedTxn(
        external_account=account,
        account_type="investment",
        date=iso_date,
        amount_cents=amount_cents or 0,
        action=action,
        symbol=fund,
        quantity=quantity,
        price=price,
        memo=_col(row, *prof.memo_cols) if prof.memo_cols else raw_action,
        fitid=_col(row, *prof.fitid_cols),
    )


# Header aliases. The cash aliases (DATE_COLS / PAYEE_COLS / AMOUNT_COLS) now live
# in mammon.importers.tabular as the single source of truth shared with the
# inference engine, and are imported above. The investment-only aliases stay here.
# investment field aliases (requirement: any two of shares/pps/total -> derive 3rd).
SHARES_COLS = ("Shares", "Quantity", "Units", "Shares/Unit", "No. of Shares")
PRICE_COLS = ("Price", "Price Per Share", "Unit Price", "Share Price", "Price ($)")
TOTAL_COLS = ("Amount", "Total", "Total Price", "Net Amount", "Amount ($)")
SYMBOL_COLS = ("Symbol", "Ticker")
SECURITY_COLS = ("Investment", "Fund", "Security", "Security Description")


def parse_csv(text: str, default_account: Optional[str] = None,
              roles: "tabular.CashRoles | None" = None,
              roles_authoritative: bool = False) -> list[NormalizedTxn]:
    """Parse a tabular export into NormalizedTxn records, on the ledger's sign
    convention.

    A thin wrapper over :func:`_parse_csv_rows` that exists for ONE reason: the
    body has four exits (profile, authoritative roles, cash, generic), and the
    sign normalisation has to happen on all of them. Hooking it before the last
    ``return`` covered the generic path only, so a plan export -- which leaves by
    the profile exit -- kept its negative fee quantities and would have posted a
    ShrsOut that ADDS shares. See record.normalize_investment_signs.
    """
    out = _parse_csv_rows(text, default_account, roles, roles_authoritative)
    normalize_investment_signs(out)
    return out


def _parse_csv_rows(text: str, default_account: Optional[str] = None,
                    roles: "tabular.CashRoles | None" = None,
                    roles_authoritative: bool = False) -> list[NormalizedTxn]:
    """Parse a tabular export into NormalizedTxn records.

    Structure detection (:func:`tabular.locate_and_frame`) finds the real header,
    dropping any preamble/footer/blank rows and honouring a sniffed delimiter and
    quoted multi-line fields -- so a real download with title rows and a legal
    footer (Venmo) no longer imports as nothing or as blank rows. Then:

    * a NAV-unit **investment column-profile** (Fidelity 401k / State 529 / T. Rowe)
      or a file whose header exposes a **known date/security alias** takes the
      LEGACY per-row path below, unchanged;
    * any other header (unknown vocabulary, e.g. Venmo's ``Datetime`` /
      ``Amount (total)`` / ``From`` / ``To``) is interpreted by the inference
      engine, optionally steered by a saved/wizard-built ``roles`` map.

    ``roles_authoritative`` marks a ``roles`` map the USER stands behind (the
    mapping wizard, or a profile saved from it). Such a map overrides the
    routing above for any cash source, so a confirmed correction is not
    thrown away just because the header looked conventional.
    """
    frame = tabular.locate_and_frame(text)
    if frame is None:
        return []
    header, rows = frame.header, frame.rows

    profile = _detect_profile(header)
    if profile is not None:
        out: list[NormalizedTxn] = []
        for row in rows:
            txn = _profile_txn(profile, row, default_account)
            if txn is not None:
                out.append(txn)
        return out

    # Route to the legacy generic path when the header speaks the known vocabulary
    # (a recognised date column) or is clearly an investment file. Everything else
    # -- an unreadable-vocabulary download -- goes through role inference.
    known_cash = tabular._pick(header, DATE_COLS) is not None
    has_investment = bool(
        tabular._pick(header, SYMBOL_COLS) or tabular._pick(header, SECURITY_COLS)
        or tabular._pick(header, SHARES_COLS))

    # An AUTHORITATIVE role map -- one the user confirmed through the mapping
    # wizard, or a saved profile built from one -- ALWAYS wins for a cash source.
    # Without this the routing above silently discarded it whenever the header
    # merely LOOKED conventional, which is exactly when an override is needed: in
    # "Date,Description,Amount,Balance" both money columns are recognisable and
    # equally money-shaped, so a user correcting Amount->Balance (or the reverse)
    # saw the fix accepted, saved, and then ignored on every subsequent import.
    if roles is not None and roles_authoritative and not has_investment:
        out = tabular.build_cash_records(frame, roles, default_account)
        _synthesize_cash_fitids(out)
        return out

    if not (known_cash or has_investment):
        used = roles or tabular.infer_cash_roles(header, rows)
        out = tabular.build_cash_records(frame, used, default_account)
        _synthesize_cash_fitids(out)
        return out

    out = []
    for row in rows:
        iso_date = _try_date(_col(row, *DATE_COLS))
        if iso_date is None:
            continue
        symbol = normalize_security_name(
            _col(row, *SYMBOL_COLS) or _col(row, *SECURITY_COLS))
        shares = _col(row, *SHARES_COLS)
        account = _col(row, "Account", "Account Name") or (default_account or "")

        # The file decides, not the row. A brokerage statement's interest and fee
        # lines carry no security and no share count, and testing each row
        # individually dropped them into the CASH branch -- losing their action
        # and description, and landing them in a different TABLE from the
        # security rows of the same file. That is the same defect as reading an
        # OFX <INVBANKTRAN> as cash: an investment statement's own cash activity
        # is still investment activity. ``has_investment`` is decided once from
        # the HEADER, so every dated row of an investment file stays together.
        # Any two of shares/pps/total let the third be derived; all three are
        # checked for consistency.
        if has_investment or symbol or shares:
            quantity, price, amount_cents, _ok = derive_investment_amounts(
                shares, _col(row, *PRICE_COLS), _col(row, *TOTAL_COLS))
            out.append(
                NormalizedTxn(
                    external_account=account,
                    account_type="investment",
                    date=iso_date,
                    amount_cents=amount_cents or 0,
                    action=(_col(row, "Action", "Transaction Type", "Type")
                            or ("Buy" if (symbol or shares) else "Cash")),
                    symbol=symbol,
                    quantity=quantity,
                    price=price,
                    commission_cents=dollars_to_cents(_col(row, "Commission", "Commission ($)", "Fees")),
                    memo=_col(row, "Description", "Memo", "Security Description"),
                    fitid=_col(row, "FITID", "Reference", "Transaction ID"),
                )
            )
        else:
            status = _col(row, "Status", "Transaction Status")
            out.append(
                NormalizedTxn(
                    external_account=account,
                    date=iso_date,
                    amount_cents=_cash_amount(row),
                    payee=_col(row, *PAYEE_COLS),
                    memo=_col(row, "Memo", "Notes", "Note"),
                    category=_col(row, "Category"),
                    check_number=_col(row, "Check", "Check Number", "Check #"),
                    fitid=_col(row, "FITID", "Reference", "Transaction ID"),
                    type=_col(row, "Type", "Transaction Type"),
                    cleared=1 if status.strip().lower() in _CLEARED_STATUS else 0,
                )
            )
    _synthesize_cash_fitids(out)
    return out


def _synthesize_cash_fitids(records: list[NormalizedTxn]) -> None:
    """Give id-less cash rows a STABLE synthetic fitid so re-imports of an
    overlapping CSV export (e.g. Wells Fargo's 25-month window, re-pulled
    monthly) dedup EXACTLY instead of relying only on fuzzy matching.

    The id is derived from the stable fields -- date, signed cents, and the
    normalized payee -- plus an occurrence index that distinguishes genuinely
    identical same-day rows. So it is reproducible across exports (the same
    physical posted transaction always hashes to the same id) yet unique within
    a file (two identical same-day charges get indices 0 and 1, and both land).
    Rows that already carry a real id, and investment rows, are left untouched.
    """
    seq: dict[tuple, int] = {}
    for r in records:
        if r.is_investment or r.fitid:
            continue
        norm = normalize_payee(r.payee)
        key = (r.date, r.amount_cents, norm)
        i = seq.get(key, 0)
        seq[key] = i + 1
        h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
        r.fitid = f"csv:{r.date}:{r.amount_cents}:{h}:{i}"


def _cash_amount(row: dict) -> int:
    """Signed cents for a cash row: a single signed amount column when present,
    otherwise separate Debit/Credit (a.k.a. Withdrawal/Deposit, Charge/Payment)
    columns folded into one signed value (debit negative, credit positive)."""
    amt = _col(row, *AMOUNT_COLS)
    if amt:
        return dollars_to_cents(amt)
    cents = 0
    debit = _col(row, "Debit", "Withdrawal", "Withdrawals", "Charge", "Debits")
    credit = _col(row, "Credit", "Deposit", "Deposits", "Payment", "Payments", "Credits")
    if debit:
        cents -= abs(dollars_to_cents(debit))
    if credit:
        cents += abs(dollars_to_cents(credit))
    return cents


# Placeholders a statement writes to mean "this field does not apply". Interactive
# Brokers fills Quantity, Price AND Symbol with a bare "-" on a dividend or a fee,
# and taking that literally imported 59 transactions whose SECURITY was named "-".
# They are not values; they are the absence of one.
_NOT_APPLICABLE = {"-", "--", "---", "n/a", "na", "none", "null"}


def _blank_placeholder(value: str) -> str:
    """"" for a not-applicable placeholder, otherwise the value unchanged."""
    return "" if value.strip().lower() in _NOT_APPLICABLE else value


def _col(row: dict, *names: str) -> str:
    for n in names:
        if n in row and row[n] != "":
            return _blank_placeholder(row[n])
    # case-insensitive fallback
    lowered = {k.lower(): v for k, v in row.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v:
            return _blank_placeholder(v)
    return ""
