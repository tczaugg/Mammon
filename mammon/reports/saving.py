"""mammon.reports.saving -- money SAVED or PAID DOWN, by destination account
(SRD 5.12d).

Spending leaves the household; saving only moves it. A 401(k) deferral split off
a paycheck, the principal leg of a mortgage payment and a transfer to a savings
account are all transfers, so every spending view excludes them -- correctly --
and until this module existed nothing counted them at all. Yet they are what a
household budgets around ("how much am I putting into the 401(k) each month?"),
so the Budget Planner reports them beside spending, never inside it.

Definitions:

- A **cash-flow account** is where household money is spent from: ``checking``,
  ``credit`` and ``cash`` (:data:`CASH_FLOW_ACCOUNT_TYPES`). Every other kind --
  savings, investment, retirement, liability, asset, crypto -- is a place money
  is saved INTO or a debt paid DOWN. ``savings`` is deliberately on the saving
  side: moving money from checking into savings is saving, which is the answer
  the user gave. (Spending out of a savings account is still spending; that is
  :data:`mammon.budgets.SPENDING_ACCOUNT_TYPES`, a different question.)
- **Saving** is a transfer leg on a cash-flow account whose other side is NOT a
  cash-flow account, split legs included. Paying a credit card from checking is
  therefore neither spending nor saving: the card's purchases were the spending,
  and counting the payment too would count them twice. A move between two
  saving-side accounts (savings to brokerage) is not counted either -- that money
  was saved once already, when it left checking.
- It is **net**, per destination account, as a positive number when money went
  in: a withdrawal from the brokerage back to checking lowers that month's
  saving, and a new loan's disbursement shows as negative pay-down. Net is what
  moves net worth, which is the point of saving.
- Only the cash-flow side is read, so each transfer is counted once, whichever
  side it was entered from: the ledger's mirror model puts one leg on each
  account, and exactly one of them is on a cash-flow account.

The line extraction is :func:`mammon.reports._lines.signed_lines`, the same one
every flow report uses, so split legs, scheduled placeholders and ledger order
follow the one set of rules. Money is signed integer cents throughout.
"""
from __future__ import annotations

from typing import Iterable, Optional

from mammon import ledger
from mammon.reports._lines import Line, signed_lines

#: Account kinds household money is SPENT from. Transfers out of these to any
#: other kind are saving or debt pay-down.
CASH_FLOW_ACCOUNT_TYPES = ("checking", "credit", "cash")


def cash_flow_account_ids(conn) -> list[int]:
    """Ids of the cash-flow accounts. Closed ones are INCLUDED, for the same
    reason :func:`mammon.budgets.spending_account_ids` includes them: last year's
    contributions out of an account closed since were still contributions."""
    return [int(a["id"]) for a in ledger.list_accounts(conn, include_closed=True)
            if (a["type"] or "") in CASH_FLOW_ACCOUNT_TYPES]


def _cash_flow_types(conn) -> dict[int, bool]:
    """``account id -> is a cash-flow account``, over EVERY account (hidden and
    closed too), so a transfer to a hidden checking account is still recognized
    as a move between cash-flow accounts rather than mistaken for saving."""
    return {int(r["id"]): (r["type"] or "") in CASH_FLOW_ACCOUNT_TYPES
            for r in ledger.list_accounts(conn, include_closed=True,
                                          include_hidden=True)}


def saving_lines(conn, start: str, end: str, *,
                 account_ids: Optional[Iterable[int]] = None,
                 include_scheduled: bool = False) -> list[Line]:
    """Every saving leg dated in ``[start, end]``, in ledger order.

    ``account_ids`` narrows the SOURCE accounts (default: every cash-flow
    account); a source that is not itself a cash-flow account is ignored, since
    money leaving a savings account for a brokerage is not new saving.
    ``include_scheduled`` keeps pending pre-entries, as everywhere else."""
    kinds = _cash_flow_types(conn)
    sources = (cash_flow_account_ids(conn) if account_ids is None
               else [int(a) for a in account_ids if kinds.get(int(a))])
    if not sources:
        return []
    return [ln for ln in signed_lines(conn, start, end, sources, transfers="all",
                                      include_scheduled=include_scheduled)
            if ln.transfer_account_id is not None
            and not kinds.get(int(ln.transfer_account_id), False)]


def saving_by_account(conn, start: str, end: str, *,
                      account_ids: Optional[Iterable[int]] = None,
                      include_scheduled: bool = False) -> dict[int, int]:
    """``destination account id -> net cents saved into it`` over the window.

    Positive means money went in (a contribution, a principal payment); negative
    means more came back out than went in. An account with no saving legs in the
    window is absent rather than zero."""
    out: dict[int, int] = {}
    for ln in saving_lines(conn, start, end, account_ids=account_ids,
                           include_scheduled=include_scheduled):
        dest = int(ln.transfer_account_id)
        out[dest] = out.get(dest, 0) - int(ln.amount)
    return out
