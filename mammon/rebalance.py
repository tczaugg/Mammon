"""mammon.rebalance -- a TARGET asset mix, and how far the real one has drifted
from it (SRD 5.8f).

:mod:`mammon.portfolio` answers "where is my money". This answers "is it where I
meant it to be", which is the question an allocation view exists to serve. Asset
classes are worth separating because they behave differently; the payoff for
holding several is keeping them at chosen weights as they diverge, and that
needs a target, a measured deviation, and a rule for when the deviation is worth
acting on.

Read-only over the ledger: nothing here writes a transaction. It writes only its
own target tables, and it PROPOSES trades -- the arithmetic that closes the gap.
Executing them stays the user's job, in the register, where it belongs.

**The accounts are the user's to choose, and they are all one kind of money.**
A target names its own accounts (``allocation_target_accounts``). Two reasons,
both reported by the user (2026-09-15):

* *"This mixes kinds of money. 401K + IRA shouldn't be mixed with ROTH which
  shouldn't be mixed with non-tax special holdings."* A dollar in a Roth is not
  a dollar in a 401(k): they are taxed differently on the way out, they are
  rebalanced separately, and averaging them hides that a Roth is all bonds. So
  each account carries a :data:`TAX_TREATMENTS` value and a target may cover
  only ONE of them.
* *"There is no customization for accounts. I wouldn't want to include the
  [529] accounts here as those are for my kids and not something I consider part
  of my assets."* Money held for someone else is not part of the mix at all, and no
  scope rule can know that -- only the person can.

This replaces the old ``sleeve`` enum, whose two values ("investments",
"investments and cash") BOTH included cash -- a brokerage's idle cash is cash --
so the difference between them was invisible while the advice changed. A target
with no account list still reads its stored sleeve, so a file made before this
keeps working until its accounts are chosen.

Property is reported ALONGSIDE, as context (:attr:`DriftReport.fixed_rows`),
never folded into the mix being corrected -- a drift number dominated by an
illiquid position is not actionable, which is the failure mode most tools avoid
only by not knowing the house exists.

**The bands.** The default is the 5/25 rule: act when a class is off by 5
absolute percentage points OR by 25% of its own target weight, whichever fires
first. Neither half works alone. Five points never fires on a 4% sleeve that has
doubled to 8%; a purely relative band fires constantly on a 60% one, where a
routine 3-point wobble is only 5% relative. Both are stored per target so a user
can loosen or tighten them.

**What this deliberately does NOT do.** It does not choose the assets, rank them
by past return, or draw an efficient frontier. Mean-variance optimization is
dominated by expected-return estimates that nobody can make reliably, and
screening candidates by trailing performance feeds it exactly the assets whose
histories are most overstated -- a portfolio built that way in early 2008 found
that six apparently uncorrelated bets were one leveraged bet on credit. Holding a
target and rebalancing to it degrades gracefully when the inputs are wrong.
Optimizing to a frontier does not.

**Tax is not modelled here.** A proposed sale in a taxable account realizes a
gain, and the cheapest rebalance is usually the one funded by new contributions
rather than sales. The lot data to do that properly already exists
(:mod:`mammon.portfolio`), but this module reports the gap, not the tax; treat
its sell figures as "how far off", not "what to sell".
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from mammon import asset_values, investments, ledger, portfolio
from mammon.portfolio import list_securities

# The sleeves a target may govern -- a subset of portfolio.ALLOCATION_SCOPES.
# "everything" is deliberately absent: a target mix including a house is a
# target you cannot rebalance to.
TARGET_SLEEVES = ("investments", "with_cash")
SLEEVE_LABELS = {
    "investments": "Investment accounts",
    "with_cash": "Investments and cash accounts",
}
# What kind of money an account holds. A target covers exactly one, because the
# tax treatment is what makes two dollars non-interchangeable.
TAX_TREATMENTS = ("taxable", "deferred", "roth", "special")
TAX_TREATMENT_LABELS = {
    "taxable": "Taxable",
    "deferred": "Tax-deferred (401k, traditional IRA)",
    "roth": "Roth (tax-free)",
    "special": "Special purpose (529, HSA, held for others)",
    "": "Not set",
}
#: Treatments whose gains are NOT capital gains to the owner. A sale inside a
#: 401(k), a traditional IRA, a Roth or a 529/HSA produces no Schedule D line and
#: no holding-period clock: the tax (if any) happens on the way OUT of the
#: wrapper, at ordinary rates or not at all, and never depends on whether the
#: shares were held a year. User, 2026-09-19: *"401K, IRA and Roth IRA do not pay
#: capital gains."* An account with no treatment recorded is assumed TAXABLE --
#: the conservative default, and the only one that keeps existing files reading
#: the way they did. Never inferred from the name: "IRA" appears in Roth IRAs too
#: and "Trust" appears in taxable ones.
CAPITAL_GAINS_EXEMPT_TREATMENTS = ("deferred", "roth", "special")
#: How far back a holding's change is measured when the target has never been
#: marked rebalanced.
DEFAULT_SINCE_DAYS = 365
DEFAULT_BAND_ABS = Decimal("5")
DEFAULT_BAND_REL = Decimal("25")
_HUNDRED = Decimal("100")


def _D(value, default: Decimal = Decimal("0")) -> Decimal:
    """Decimal from stored text / a number, tolerant of None and junk."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value).strip().rstrip("%").strip())
    except Exception:
        return default


def _pct_text(value) -> str:
    """A percentage as storage text. Rejects the impossible rather than storing
    it: a negative weight is not a mix, and over 100 cannot be part of one."""
    pct = _D(value, None)
    if pct is None:
        raise ValueError(f"not a percentage: {value!r}")
    if pct < 0 or pct > _HUNDRED:
        raise ValueError(f"a target percentage must be 0-100, got {pct}")
    # format(), never Decimal.normalize(): normalize turns 70 into "7E+1", which
    # is the same stored-exponent bug that made an option multiplier read 1E+2.
    text = format(pct.normalize(), "f")
    return text


def _cents(total: int, pct: Decimal) -> int:
    """``pct`` percent of ``total`` cents, ROUND_HALF_UP at the cents boundary
    (the locked money convention)."""
    return int((Decimal(int(total)) * pct / _HUNDRED).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------
def create_target(conn, name: str, *, sleeve: str = "investments",
                  band_abs_pct=DEFAULT_BAND_ABS, band_rel_pct=DEFAULT_BAND_REL,
                  lines: Optional[dict] = None, active: bool = False) -> int:
    """Create a named target mix. ``lines`` is ``{asset_class: pct}``."""
    if sleeve not in TARGET_SLEEVES:
        raise ValueError(f"unknown sleeve {sleeve!r}; one of {TARGET_SLEEVES}")
    if not (name or "").strip():
        raise ValueError("a target needs a name")
    cur = conn.execute(
        "INSERT INTO allocation_targets(name, active, sleeve, band_abs_pct, band_rel_pct) "
        "VALUES (?,?,?,?,?)",
        (name.strip(), 0, sleeve, _pct_text(band_abs_pct), _pct_text(band_rel_pct)))
    target_id = int(cur.lastrowid)
    if lines:
        set_lines(conn, target_id, lines)
    conn.commit()
    if active:
        set_active(conn, target_id)
    return target_id


def update_target(conn, target_id: int, **fields) -> None:
    """Change a target's name, sleeve or bands."""
    allowed = {"name", "sleeve", "band_abs_pct", "band_rel_pct"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown target field(s): {', '.join(sorted(bad))}")
    updates = {}
    for key, value in fields.items():
        if key == "sleeve":
            if value not in TARGET_SLEEVES:
                raise ValueError(f"unknown sleeve {value!r}; one of {TARGET_SLEEVES}")
            updates[key] = value
        elif key == "name":
            if not (value or "").strip():
                raise ValueError("a target needs a name")
            updates[key] = value.strip()
        else:
            updates[key] = _pct_text(value)
    if not updates:
        return
    sets = ",".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE allocation_targets SET {sets} WHERE id=?",
                 (*updates.values(), int(target_id)))
    conn.commit()


def list_targets(conn) -> list:
    return list(conn.execute(
        "SELECT * FROM allocation_targets ORDER BY active DESC, name").fetchall())


def get_target(conn, target_id: int):
    return conn.execute("SELECT * FROM allocation_targets WHERE id=?",
                        (int(target_id),)).fetchone()


def delete_target(conn, target_id: int) -> bool:
    cur = conn.execute("DELETE FROM allocation_targets WHERE id=?", (int(target_id),))
    conn.commit()
    return cur.rowcount > 0


def set_active(conn, target_id) -> None:
    """Make one target active (``None`` deactivates all). At most one is active,
    so the drift view always knows which mix it is measuring against."""
    conn.execute("UPDATE allocation_targets SET active=0")
    if target_id is not None:
        conn.execute("UPDATE allocation_targets SET active=1 WHERE id=?",
                     (int(target_id),))
    conn.commit()


def active_target(conn):
    return conn.execute(
        "SELECT * FROM allocation_targets WHERE active=1 ORDER BY id LIMIT 1").fetchone()


# ---------------------------------------------------------------------------
# what kind of money an account holds, and which accounts a target governs
# ---------------------------------------------------------------------------
def account_treatment(acct) -> str:
    """The tax treatment recorded for an account row, or ``""`` when unset.
    Never guessed from the name: "IRA" appears in Roth IRAs too."""
    try:
        value = (acct["tax_treatment"] or "").strip().lower()
    except (KeyError, IndexError, TypeError):
        return ""
    return value if value in TAX_TREATMENTS else ""


def is_capital_gains_exempt(acct) -> bool:
    """True when this account's gains never reach a Schedule D -- a 401(k), a
    traditional IRA, a Roth, a 529 or an HSA (:data:`CAPITAL_GAINS_EXEMPT_TREATMENTS`).

    One function so the Capital Gains report and the rebalancer agree on what a
    retirement dollar is; a second copy keyed off the account NAME is the bug the
    user reported ("401K, IRA and Roth IRA do not pay capital gains"). Unset
    treatment reads as taxable, so nothing changes for a file that never said."""
    return account_treatment(acct) in CAPITAL_GAINS_EXEMPT_TREATMENTS


def set_account_treatment(conn, account_id: int, treatment) -> None:
    """Record what kind of money an account holds (``None`` clears it). Written
    through :mod:`mammon.ledger`, which owns the accounts table."""
    value = (treatment or "").strip().lower() or None
    if value is not None and value not in TAX_TREATMENTS:
        raise ValueError(f"unknown tax treatment {treatment!r}; one of {TAX_TREATMENTS}")
    ledger.update_account(conn, int(account_id), tax_treatment=value)


def accounts_for_picking(conn, include_hidden: bool = False) -> list:
    """Every account a target could cover -- the investment-like and cash-shaped
    ones -- as ``(id, name, treatment)``, in the ledger's own order. A liability
    is never offered: an allocation is of what you own."""
    kinds = ledger.INVESTMENT_LIKE_TYPES + ("checking", "savings", "cash")
    return [(int(a["id"]), a["name"], account_treatment(a))
            for a in ledger.list_accounts(conn, include_closed=False,
                                          include_hidden=include_hidden)
            if (a["type"] or "") in kinds]


def target_accounts(conn, target_id: int) -> list:
    """The account ids this target governs (empty = none chosen; the legacy
    ``sleeve`` then decides, so an older file keeps working)."""
    return [int(r["account_id"]) for r in conn.execute(
        "SELECT account_id FROM allocation_target_accounts WHERE target_id=? "
        "ORDER BY account_id", (int(target_id),))]


def mixed_treatments(conn, account_ids) -> list:
    """The distinct tax treatments among these accounts, ignoring unset ones.
    More than one is what :func:`set_target_accounts` refuses."""
    seen = []
    for aid in account_ids or ():
        acct = ledger.get_account(conn, int(aid))
        t = account_treatment(acct) if acct is not None else ""
        if t and t not in seen:
            seen.append(t)
    return seen


def set_target_accounts(conn, target_id: int, account_ids) -> None:
    """Replace the target's account list.

    Refused when the chosen accounts hold more than one kind of money: a target
    is a mix of dollars that are interchangeable, and a Roth dollar and a
    401(k) dollar are not. Accounts whose treatment is unset are allowed --
    saying so is a separate decision, made in Account Details."""
    ids = [int(a) for a in account_ids or ()]
    kinds = mixed_treatments(conn, ids)
    if len(kinds) > 1:
        names = ", ".join(TAX_TREATMENT_LABELS.get(k, k) for k in kinds)
        raise ValueError(
            f"a target covers one kind of money at a time, and these accounts "
            f"hold {len(kinds)}: {names}. Make a target for each.")
    conn.execute("DELETE FROM allocation_target_accounts WHERE target_id=?",
                 (int(target_id),))
    for aid in ids:
        conn.execute("INSERT OR IGNORE INTO allocation_target_accounts"
                     "(target_id, account_id) VALUES (?,?)", (int(target_id), aid))
    conn.commit()


def set_rebalanced(conn, target_id: int, date=None) -> str:
    """Mark the target rebalanced on ``date`` (today by default). That date is
    the span each holding's change is measured over -- what has moved since you
    last acted is exactly what pushed a class off target."""
    when = date or _dt.date.today().isoformat()
    _dt.date.fromisoformat(when)
    conn.execute("UPDATE allocation_targets SET rebalanced_on=? WHERE id=?",
                 (when, int(target_id)))
    conn.commit()
    return when


# ---------------------------------------------------------------------------
# lines
# ---------------------------------------------------------------------------
def set_line(conn, target_id: int, asset_class: str, pct) -> None:
    """Set (or, with a zero/blank pct, clear) one class's target weight."""
    if asset_class not in portfolio.ASSET_CLASSES:
        raise ValueError(f"unknown asset class {asset_class!r}; one of "
                         f"{portfolio.ASSET_CLASSES}")
    # CLEARING is explicit (None, blank, zero). Anything else must parse: a
    # typo in a percentage field silently deleting the line would remove a
    # target weight the user believes they just set.
    blank = pct is None or (isinstance(pct, str) and not pct.strip())
    value = Decimal("0") if blank else _D(pct, None)
    if value is None:
        raise ValueError(f"not a percentage: {pct!r}")
    if value == 0 and not is_locked(conn, target_id, asset_class):
        conn.execute("DELETE FROM allocation_target_lines "
                     "WHERE target_id=? AND asset_class=?",
                     (int(target_id), asset_class))
    elif value == 0:
        # A LOCKED line at zero is a decision ("hold nothing here"), not an
        # empty field: deleting it would throw the lock away and let the next
        # redistribution hand the class weight the user just refused.
        conn.execute("UPDATE allocation_target_lines SET pct='0' "
                     "WHERE target_id=? AND asset_class=?",
                     (int(target_id), asset_class))
    else:
        conn.execute(
            "INSERT INTO allocation_target_lines(target_id, asset_class, pct) "
            "VALUES (?,?,?) ON CONFLICT(target_id, asset_class) "
            "DO UPDATE SET pct=excluded.pct",
            (int(target_id), asset_class, _pct_text(value)))
    conn.commit()


def set_lines(conn, target_id: int, mapping: dict) -> None:
    """Replace every line on the target with ``mapping``."""
    conn.execute("DELETE FROM allocation_target_lines WHERE target_id=?",
                 (int(target_id),))
    for asset_class, pct in (mapping or {}).items():
        set_line(conn, target_id, asset_class, pct)
    conn.commit()


def target_lines(conn, target_id: int) -> dict:
    """``{asset_class: Decimal pct}`` for the target."""
    return {r["asset_class"]: _D(r["pct"]) for r in conn.execute(
        "SELECT asset_class, pct FROM allocation_target_lines WHERE target_id=? "
        "ORDER BY asset_class", (int(target_id),)).fetchall()}


def target_total(conn, target_id: int) -> Decimal:
    """What the target's weights add up to. A mix that is not 100 is a mistake
    worth SHOWING rather than silently normalizing away -- normalizing would
    turn a forgotten line into a plausible, wrong target."""
    return sum(target_lines(conn, target_id).values(), Decimal("0"))


# ---------------------------------------------------------------------------
# locks and the always-100 edit (SRD 5.8f)
# ---------------------------------------------------------------------------
# A mix that does not add up to 100 is not a mix, and the old editor let the
# user build one a keystroke at a time. The fix is not validation after the
# fact but an edit that cannot leave the total: raising one class LOWERS the
# others. The lock is what makes that livable -- without it, the class settled
# three edits ago drifts back out from under the user. Locked classes are never
# touched, so the workflow is: set a class, lock it, move on.
_PCT_Q = Decimal("0.01")       # weights are stored to the cent of a percent


def locked_classes(conn, target_id: int) -> set:
    """The asset classes whose weight the user has pinned on this target."""
    return {r["asset_class"] for r in conn.execute(
        "SELECT asset_class FROM allocation_target_lines "
        "WHERE target_id=? AND locked=1", (int(target_id),)).fetchall()}


def is_locked(conn, target_id: int, asset_class: str) -> bool:
    row = conn.execute(
        "SELECT locked FROM allocation_target_lines "
        "WHERE target_id=? AND asset_class=?",
        (int(target_id), asset_class)).fetchone()
    return bool(row and row["locked"])


def set_locked(conn, target_id: int, asset_class: str, locked: bool) -> None:
    """Pin (or release) one class's weight.

    Locking a class that has no line yet writes a zero line, so "locked at 0%"
    survives: the lock is a statement about the class, and a class with no row
    would otherwise be handed weight by the next redistribution.
    """
    if asset_class not in portfolio.ASSET_CLASSES:
        raise ValueError(f"unknown asset class {asset_class!r}; one of "
                         f"{portfolio.ASSET_CLASSES}")
    flag = 1 if locked else 0
    cur = conn.execute(
        "UPDATE allocation_target_lines SET locked=? "
        "WHERE target_id=? AND asset_class=?",
        (flag, int(target_id), asset_class))
    if cur.rowcount == 0 and flag:
        conn.execute(
            "INSERT INTO allocation_target_lines(target_id, asset_class, pct, "
            "locked) VALUES (?,?,'0',1)", (int(target_id), asset_class))
    conn.commit()


def apply_target_edit(lines: dict, locked, asset_class: str, pct) -> dict:
    """Set ``asset_class`` to ``pct`` and rebalance the rest to total exactly 100.

    Pure Decimal arithmetic over ``{asset_class: pct}``; no database, no float.

    The locked classes keep their exact stored values. What is left of 100 after
    them is split between the edited class and the other UNLOCKED classes, in
    proportion to what those already held (equally, if they are all at zero --
    proportional sharing of nothing gives nothing, and the total would break).
    The edit is CLAMPED into ``0 .. 100 - locked``: an edit that cannot be
    absorbed is trimmed, never allowed to push the total off 100, because a
    silently wrong total is worse than a value that stops where it must.
    """
    locked = set(locked or ())
    out = {k: _D(v) for k, v in (lines or {}).items()}
    out.setdefault(asset_class, Decimal("0"))
    want = _D(pct, None)
    if want is None:
        raise ValueError(f"not a percentage: {pct!r}")

    fixed = {k: v for k, v in out.items() if k in locked and k != asset_class}
    room = _HUNDRED - sum(fixed.values(), Decimal("0"))
    if room < 0:                      # locked lines already over 100: nothing free
        room = Decimal("0")
    new = min(max(want, Decimal("0")), room).quantize(_PCT_Q, ROUND_HALF_UP)

    others = [k for k in out if k != asset_class and k not in locked]
    share = room - new
    if not others:
        # No one to absorb the change: the edited class IS the remainder.
        new = room.quantize(_PCT_Q, ROUND_HALF_UP)
        share = Decimal("0")
    result = dict(fixed)
    result[asset_class] = new
    if others:
        old_total = sum((out[k] for k in others), Decimal("0"))
        if old_total > 0:
            for k in others:
                result[k] = (share * out[k] / old_total).quantize(
                    _PCT_Q, ROUND_HALF_UP)
        else:
            even = (share / len(others)).quantize(_PCT_Q, ROUND_HALF_UP)
            for k in others:
                result[k] = even
        # Rounding remainder lands on the largest unlocked recipient, the same
        # convention target_from_current uses, so the column sums to 100 exactly.
        drift_pp = _HUNDRED - sum(result.values(), Decimal("0"))
        if drift_pp:
            biggest = max(others, key=lambda k: (result[k], k))
            result[biggest] = max(Decimal("0"), result[biggest] + drift_pp)
    return result


def set_line_balanced(conn, target_id: int, asset_class: str, pct) -> dict:
    """Write one class's weight, rebalancing the unlocked rest to total 100.

    The single write path for the Target & Drift editor; the UI never computes
    a weight itself.
    """
    current = target_lines(conn, target_id)
    updated = apply_target_edit(current, locked_classes(conn, target_id),
                                asset_class, pct)
    for cls, value in updated.items():
        if current.get(cls) != value:
            set_line(conn, target_id, cls, value)
    return updated


# ---------------------------------------------------------------------------
# targets stated PER FUND (SRD 5.8f-1)
# ---------------------------------------------------------------------------
# A weight per asset class is not a tradeable instruction: a blended fund moves
# three classes at once, so "sell $30,000 of domestic stock" has to be
# decomposed across holdings whose mixes differ, using preferences the app does
# not have. Stated per FUND it is directly executable -- "FXAIX is 22%, target
# 25%, buy 3%" -- it produces the buy-low/sell-high effect by construction, and
# it restores the class mix as a consequence rather than as an aim.
#
# The class mix therefore stops being the instruction and becomes the CHECK:
# these functions compute, forward and exactly, what a set of fund weights
# implies. No optimizer, no lot selection, nothing the user did not state.
#
# Percent is of the ACCOUNT. An account is the unit you can trade within --
# money does not move between a 401(k) and a taxable account -- which is also
# how a broker's auto-rebalance is configured, so the same numbers can be typed
# there.
@dataclass
class FundDrift:
    """One fund's distance from its target inside one account."""
    account_id: int
    account_name: str
    symbol: str
    security_name: Optional[str]
    target_pct: Decimal
    current_pct: Decimal
    target_cents: int
    current_cents: int
    move_cents: int                  # + buy, - sell
    priced: bool = True

    @property
    def drift_pct(self) -> Decimal:
        return self.current_pct - self.target_pct

    @property
    def action(self) -> str:
        if self.move_cents == 0:
            return "hold"
        return "buy" if self.move_cents > 0 else "sell"


@dataclass
class FundTargetReport:
    """What a per-fund target says, and what it would do to the whole portfolio.

    ``blend`` and ``current_blend`` are about the TARGET ACCOUNTS alone -- the
    part the user controls. ``portfolio_before`` and ``portfolio_after`` are
    about everything they own, because a 401(k) set in isolation can be locally
    right and globally wrong, and that whole-picture view is the thing the user
    reports never having had.
    """
    target_id: int
    as_of: str
    account_ids: list = field(default_factory=list)
    funds: list = field(default_factory=list)          # list[FundDrift]
    account_totals: dict = field(default_factory=dict)  # {account_id: cents}
    account_pct_totals: dict = field(default_factory=dict)  # {account_id: Decimal}
    current_blend: dict = field(default_factory=dict)   # {class: cents}, target accts now
    blend: dict = field(default_factory=dict)           # {class: cents}, at target
    portfolio_before: dict = field(default_factory=dict)
    portfolio_after: dict = field(default_factory=dict)
    unpriced: list = field(default_factory=list)

    def pct(self, cents_by_class: dict) -> dict:
        """A cents-by-class mapping as percentages summing to 100."""
        total = sum(cents_by_class.values())
        if not total:
            return {}
        return {k: (Decimal(v) / Decimal(total) * _HUNDRED) for k, v in
                cents_by_class.items()}

    @property
    def accounts_complete(self) -> list:
        """Accounts whose fund weights do NOT add to 100, as
        ``[(account_id, total)]``. Shown rather than normalized, the same rule
        :func:`target_total` follows: normalizing a forgotten line turns a
        mistake into a plausible, wrong target."""
        return [(aid, total) for aid, total in sorted(self.account_pct_totals.items())
                if total != _HUNDRED]


def set_fund_line(conn, target_id: int, account_id: int, symbol: str, pct) -> None:
    """Set (or, with zero/blank, clear) one fund's target weight in one account."""
    sym = (symbol or "").strip()
    if not sym:
        raise ValueError("a fund line needs a symbol")
    blank = pct is None or (isinstance(pct, str) and not pct.strip())
    value = Decimal("0") if blank else _D(pct, None)
    if value is None:
        raise ValueError(f"not a percentage: {pct!r}")
    if value < 0:
        raise ValueError("a target weight cannot be negative")
    if value == 0:
        conn.execute("DELETE FROM allocation_target_funds "
                     "WHERE target_id=? AND account_id=? AND symbol=?",
                     (int(target_id), int(account_id), sym))
    else:
        conn.execute(
            "INSERT INTO allocation_target_funds(target_id, account_id, symbol, pct) "
            "VALUES (?,?,?,?) ON CONFLICT(target_id, account_id, symbol) "
            "DO UPDATE SET pct=excluded.pct",
            (int(target_id), int(account_id), sym, str(value)))
    conn.commit()


def fund_lines(conn, target_id: int) -> dict:
    """``{(account_id, symbol): Decimal pct}`` for the target."""
    return {(int(r["account_id"]), r["symbol"]): _D(r["pct"]) for r in conn.execute(
        "SELECT account_id, symbol, pct FROM allocation_target_funds "
        "WHERE target_id=? ORDER BY account_id, symbol", (int(target_id),))}


def set_fund_lines(conn, target_id: int, mapping: dict) -> None:
    """Replace every fund line on the target. Keys are ``(account_id, symbol)``."""
    conn.execute("DELETE FROM allocation_target_funds WHERE target_id=?",
                 (int(target_id),))
    for (aid, sym), pct in (mapping or {}).items():
        set_fund_line(conn, target_id, aid, sym, pct)
    conn.commit()


def has_fund_lines(conn, target_id: int) -> bool:
    """Whether this target states its weights in funds. A target does one or the
    other: maintaining both would be two statements of intent that can disagree,
    with nothing to say which wins."""
    row = conn.execute("SELECT 1 FROM allocation_target_funds WHERE target_id=? "
                       "LIMIT 1", (int(target_id),)).fetchone()
    return row is not None


def _class_split(conn, symbol: str, cents: int, mixtures: dict,
                 classes: dict) -> dict:
    """``cents`` of one security spread over its asset classes.

    The ONE place this module turns a holding into classes, and it defers to
    ``security_mix`` exactly as ``portfolio.allocation`` does, so a fund target's
    computed blend and the allocation report cannot disagree about what a fund
    is made of."""
    from mammon import security_mix
    mix = mixtures.get(symbol)
    if mix:
        return security_mix.split_value(cents, mix)
    return {classes.get(symbol) or "unclassified": cents}


def fund_target(conn, target_id: Optional[int] = None, as_of: Optional[str] = None,
                prices: Optional[dict] = None) -> FundTargetReport:
    """What the per-fund weights say, what class mix they imply, and what moving
    to them would do to the whole portfolio.

    Forward only. Every number here follows from weights the user typed; nothing
    is solved for and nothing is recommended beyond "this fund is N% and you
    said M%".
    """
    from mammon import security_mix
    target = get_target(conn, target_id) if target_id is not None else active_target(conn)
    if target is None:
        raise ValueError("no allocation target to measure against")
    tid = int(target["id"])
    on = as_of or _dt.date.today().isoformat()
    lines = fund_lines(conn, tid)
    chosen = target_accounts(conn, tid) or sorted(
        {aid for aid, _sym in lines})

    mixtures = security_mix.all_mixtures(conn)
    classes = {r["symbol"]: r["asset_class"] for r in list_securities(conn)}

    funds: list = []
    account_totals: dict = {}
    pct_totals: dict = {}
    current_blend: dict = {}
    blend: dict = {}
    unpriced: list = []

    for aid in chosen:
        val = portfolio.account_valuation(conn, aid, on, prices)
        acct = ledger.get_account(conn, aid)
        name = acct["name"] if acct is not None else str(aid)
        total = int(val.total)
        account_totals[aid] = total
        held = {h.symbol: h for h in val.holdings}
        for h in val.holdings:
            if h.price is None:
                unpriced.append(h.symbol)
            for cls, part in _class_split(conn, h.symbol, int(h.market_value),
                                          mixtures, classes).items():
                current_blend[cls] = current_blend.get(cls, 0) + part
        if val.cash:
            current_blend["cash"] = current_blend.get("cash", 0) + int(val.cash)

        symbols = sorted({sym for (a, sym) in lines if a == aid} | set(held))
        pct_totals[aid] = sum((lines.get((aid, s), Decimal("0")) for s in symbols),
                              Decimal("0"))
        for sym in symbols:
            pct = lines.get((aid, sym), Decimal("0"))
            target_cents = _cents(total, pct)
            current_cents = int(held[sym].market_value) if sym in held else 0
            row = FundDrift(
                account_id=aid, account_name=name, symbol=sym,
                security_name=None,
                target_pct=pct,
                current_pct=((Decimal(current_cents) / Decimal(total)) * _HUNDRED)
                if total else Decimal("0"),
                target_cents=target_cents, current_cents=current_cents,
                move_cents=target_cents - current_cents,
                priced=(sym not in held or held[sym].price is not None),
            )
            funds.append(row)
            for cls, part in _class_split(conn, sym, target_cents, mixtures,
                                          classes).items():
                blend[cls] = blend.get(cls, 0) + part
        # Whatever the weights do not spend stays as cash. The user does not
        # hold cash deliberately in a retirement account -- theirs is
        # un-reinvested dividends -- so weights summing to 100 sweep it away,
        # and weights summing to less leave the remainder visible as cash
        # rather than silently scaling the funds up to fill the account.
        unspent = total - sum(_cents(total, lines.get((aid, s), Decimal("0")))
                              for s in symbols)
        if unspent:
            blend["cash"] = blend.get("cash", 0) + unspent

    # The whole picture. Accounts outside the target keep exactly what they hold;
    # only the chosen ones move. This is the view the user reports never having
    # had: a 401(k) set in isolation can be locally right and globally wrong.
    portfolio_before: dict = {}
    portfolio_after: dict = {}
    for aid in portfolio.scope_account_ids(conn, "investments"):
        val = portfolio.account_valuation(conn, aid, on, prices)
        here: dict = {}
        for h in val.holdings:
            for cls, part in _class_split(conn, h.symbol, int(h.market_value),
                                          mixtures, classes).items():
                here[cls] = here.get(cls, 0) + part
        if val.cash:
            here["cash"] = here.get("cash", 0) + int(val.cash)
        for cls, cents in here.items():
            portfolio_before[cls] = portfolio_before.get(cls, 0) + cents
        if aid not in account_totals:
            for cls, cents in here.items():
                portfolio_after[cls] = portfolio_after.get(cls, 0) + cents
    for cls, cents in blend.items():
        portfolio_after[cls] = portfolio_after.get(cls, 0) + cents

    return FundTargetReport(
        target_id=tid, as_of=on, account_ids=list(chosen), funds=funds,
        account_totals=account_totals, account_pct_totals=pct_totals,
        current_blend=current_blend, blend=blend,
        portfolio_before=portfolio_before, portfolio_after=portfolio_after,
        unpriced=sorted(set(unpriced)),
    )


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------
@dataclass
class HoldingDrift:
    """One holding inside an asset class: what it is worth, how much of the class
    it is, and how much it has changed since the target was last rebalanced.

    User, 2026-09-15: "If the goal is to rebalance, by selling asset classes that
    are above target and buying those below target, wouldn't it also make sense
    to show which assets within the asset class have changed the most as those
    may be the ones we'd want to sell/buy?" ``change_cents`` is that holding's
    total return over the span (dividends included,
    :func:`mammon.portfolio.holding_performances`), and ``None`` when it cannot
    be measured -- an unpriced holding, or one bought since."""
    symbol: str
    account: str
    account_id: int
    value_cents: int
    pct_of_class: Decimal
    change_cents: Optional[int] = None
    change_pct: Optional[Decimal] = None
    priced: bool = True


@dataclass
class ClassDrift:
    """One asset class: where it is, where it should be, and the gap."""
    asset_class: str
    label: str
    current_cents: int
    current_pct: Decimal
    target_pct: Decimal
    target_cents: int
    out_of_band: bool
    holdings: list = field(default_factory=list)      # HoldingDrift, biggest first

    @property
    def is_unclassified(self) -> bool:
        """The bucket for holdings whose asset class nobody has said yet. It is
        not a class you can hold or trade, so it is never judged against a band
        and never given a buy or sell: the answer is to classify it."""
        return self.asset_class == "unclassified"

    @property
    def drift_pct(self) -> Decimal:
        """Signed percentage POINTS from target: positive is overweight."""
        return self.current_pct - self.target_pct

    @property
    def drift_rel_pct(self) -> Optional[Decimal]:
        """Signed drift as a percentage OF the target weight, or None when the
        target is zero (where a relative figure is undefined, not infinite)."""
        if self.target_pct == 0:
            return None
        return (self.drift_pct / self.target_pct) * _HUNDRED

    @property
    def move_cents(self) -> int:
        """Cents to move to return to target: positive buy, negative sell."""
        return self.target_cents - self.current_cents

    @property
    def is_cash(self) -> bool:
        """Cash is the one class that cannot be traded to hit its own weight."""
        return self.asset_class == portfolio.CASH_CLASS

    @property
    def action(self) -> str:
        """The rebalancing verb for this row, derived from ``move_cents``.

        ``classify`` for the unclassified bucket: telling someone to SELL six
        figures of "Unclassified" is advice about a gap in the records, not
        about the portfolio (user-reported).

        For a security class it is the obvious ``buy`` (underweight) or ``sell``
        (overweight). CASH is different and must NEVER be ``sell``: you cannot
        sell cash, you spend it. It is consumed and generated only as the
        by-product of the securities trades -- when cash is OVER target the
        surplus goes into the underweight securities (``spend``), and when it is
        UNDER target you ``raise`` it by selling the overweight ones. A
        rebalance is therefore set up NOT to be cash-neutral on purpose: the
        cash line's move drives how far the securities trades diverge. ``hold``
        when the class already sits on its target.

        ``spend``, not ``invest``. Every other row's verb acts on THAT row's
        class -- "Buy" the domestic stock, "Sell" the bonds -- so "Invest" on
        the cash row parsed as an instruction to invest INTO cash, which is the
        one thing a rebalance never does (reported: "now it's telling me to
        invest in cash"). "Spend" acts on cash the same way the others act on
        theirs, and pairs with "Raise" as its plain opposite.
        """
        if self.is_unclassified:
            return "classify"
        if self.move_cents == 0:
            return "hold"
        if self.is_cash:
            return "spend" if self.move_cents < 0 else "raise"
        return "buy" if self.move_cents > 0 else "sell"


#: How each :attr:`ClassDrift.action` reads to a person. Here rather than in a
#: UI because two surfaces render it -- the Target & Drift dialog and the
#: Investment Center's card, which was showing the raw lowercase verb -- and a
#: word this easy to misread should be defined once.
ACTION_LABELS = {
    "buy": "Buy", "sell": "Sell",
    "spend": "Spend", "raise": "Raise",
    "hold": "Hold", "classify": "Classify",
}

#: Why the cash row's verb is not the others'. Shown as a tooltip beside it:
#: the figure is right but its DIRECTION is the thing a reader has to get, and
#: the cell has room for a word, not a sentence.
CASH_ACTION_NOTE = {
    "spend": ("You hold more cash than the target. It is spent on the buys "
              "above -- cash is never bought, only used."),
    "raise": ("You hold less cash than the target. It is raised by the sells "
              "above -- cash is never sold, only produced."),
}


def action_label(action: str) -> str:
    """The human verb for a drift action. Unknown actions pass through
    capitalized rather than raising: a label is not worth a crash."""
    return ACTION_LABELS.get(action, (action or "").capitalize())


@dataclass
class DriftReport:
    """The whole comparison: the sleeve being measured, its classes, and the
    fixed holdings shown beside it for context."""
    target_id: Optional[int]
    target_name: str
    sleeve: str
    as_of: Optional[str]
    sleeve_total: int
    band_abs_pct: Decimal
    band_rel_pct: Decimal
    target_total_pct: Decimal
    rows: list = field(default_factory=list)          # ClassDrift, largest current first
    fixed_rows: list = field(default_factory=list)    # (label, cents) -- context only
    fixed_total: int = 0
    # The accounts the sleeve actually covers, largest first. Reported so a
    # surprising Cash figure can be checked against its source rather than
    # guessed at: brokerage cash is cash, and looks identical to a bank balance
    # that should not be in the sleeve at all.
    sleeve_accounts: list = field(default_factory=list)
    # The accounts this target actually governs, and the one kind of money they
    # hold (blank when unset). Chosen by the user, not by a scope rule.
    account_ids: list = field(default_factory=list)
    tax_treatment: str = ""
    # The span each holding's change is measured over, and whether it is the
    # date the user last rebalanced (else a fallback window).
    since: Optional[str] = None
    since_is_rebalance: bool = False
    # Holdings the sleeve could not price: they count as zero everywhere, so a
    # report that stayed silent about them was quietly wrong (a wallet's coins
    # valued nothing because their prices were filed under the wrong symbol).
    unpriced: list = field(default_factory=list)
    # Sleeve accounts holding a balance but no securities (see
    # portfolio.Allocation.cash_only_accounts). They land wholly in Cash, so a
    # target's Cash line reads wildly overweight for a reason that is a data gap,
    # not a portfolio decision.
    cash_only_accounts: list = field(default_factory=list)

    @property
    def needs_rebalance(self) -> bool:
        return any(r.out_of_band for r in self.rows)

    @property
    def unclassified_row(self):
        """The unclassified bucket, when the sleeve holds one."""
        return next((r for r in self.rows if r.is_unclassified), None)

    @property
    def out_of_band(self) -> list:
        return [r for r in self.rows if r.out_of_band]

    @property
    def target_is_complete(self) -> bool:
        """Whether the target's weights sum to 100."""
        return self.target_total_pct == _HUNDRED

    @property
    def to_move_cents(self) -> int:
        """The size of the rebalance: total cents that would change hands (each
        trade counted once, so the buys -- which equal the sells). The
        unclassified bucket is not a trade and is left out."""
        return sum(r.move_cents for r in self.rows
                   if r.move_cents > 0 and not r.is_unclassified)


def in_band(drift_pp: Decimal, target_pct: Decimal, band_abs: Decimal,
            band_rel: Decimal) -> bool:
    """The 5/25 rule: a class is IN band while it is within ``band_abs``
    percentage points AND within ``band_rel`` percent of its own target weight.

    Whichever threshold is tighter therefore governs, which is the point: five
    points never fires on a 4% sleeve that has doubled, and a relative band alone
    fires constantly on a 60% one. With a target of zero the relative test is
    undefined and only the absolute one applies."""
    magnitude = abs(drift_pp)
    if magnitude >= band_abs:
        return False
    if target_pct > 0 and (magnitude / target_pct) * _HUNDRED >= band_rel:
        return False
    return True


def _holdings_by_class(conn, alloc, as_of=None, prices=None, since=None) -> dict:
    """``{asset_class: [HoldingDrift]}`` for the sleeve, biggest first.

    What is INSIDE each class, so the rows a rebalance would trade are visible
    rather than implied: which holding grew most since the last rebalance is
    which one made the class overweight. A security split across classes
    (:mod:`mammon.security_mix`) contributes its parts to each, and its change is
    split the same way, so the pieces still sum to the holding.
    """
    from mammon import crypto, security_mix
    classes = {r["symbol"]: r["asset_class"] for r in list_securities(conn)}
    mixtures = security_mix.all_mixtures(conn)
    out: dict = {}
    for slice_ in alloc.by_account:
        aid, name = int(slice_.key), slice_.label
        acct = ledger.get_account(conn, aid)
        if acct is None or (acct["type"] or "") not in ledger.INVESTMENT_LIKE_TYPES:
            continue
        is_crypto = crypto.is_crypto_account(acct)
        valuation = (crypto.account_valuation(conn, aid, as_of, prices) if is_crypto
                     else investments.account_valuation(conn, aid, as_of, prices))
        gains = {}
        if since and not is_crypto:
            try:
                gains = portfolio.holding_performances(
                    conn, aid, as_of or _dt.date.today().isoformat(), start=since,
                    prices=prices)
            except Exception:                      # pragma: no cover - never block the view
                gains = {}
        for h in valuation.holdings:
            if not is_crypto and investments.is_option(conn, h.symbol):
                continue                            # options are out of the mix (5.8e-9)
            priced = h.price is not None
            value = int(h.market_value)
            perf = gains.get(investments.resolve_symbol(conn, h.symbol))
            parts = (security_mix.split_value(value, mixtures[h.symbol])
                     if mixtures.get(h.symbol) else
                     {classes.get(h.symbol) or "unclassified": value})
            for cls, part in parts.items():
                share = (Decimal(part) / Decimal(value)) if value else Decimal(0)
                change = None if perf is None else int(
                    (Decimal(perf.gain) * share).quantize(Decimal("1"),
                                                          rounding=ROUND_HALF_UP))
                pct = None
                if perf is not None and perf.gain_pct is not None:
                    pct = perf.gain_pct
                out.setdefault(cls, []).append(HoldingDrift(
                    symbol=h.symbol, account=name, account_id=aid,
                    value_cents=part, pct_of_class=Decimal(0),
                    change_cents=change, change_pct=pct, priced=priced))
        if valuation.cash:
            out.setdefault("cash", []).append(HoldingDrift(
                symbol="Cash", account=name, account_id=aid,
                value_cents=int(valuation.cash), pct_of_class=Decimal(0)))
    for cls, items in out.items():
        items.sort(key=lambda h: -h.value_cents)
        total = sum(h.value_cents for h in items)
        for h in items:
            h.pct_of_class = ((Decimal(h.value_cents) / Decimal(total)) * _HUNDRED
                              if total else Decimal(0))
    return out


def drift(conn, target_id: Optional[int] = None, as_of: Optional[str] = None,
          prices: Optional[dict] = None, since: Optional[str] = None, *,
          include_empty_classes: bool = False) -> DriftReport:
    """Compare the real mix against a target (the ACTIVE one when ``target_id``
    is omitted).

    Percentages are of the target's SLEEVE, not of everything owned; property and
    other fixed assets come back in ``fixed_rows`` as context. Raises when there
    is no target to measure against -- an empty report would read as "on target".

    ``include_empty_classes`` adds a row for every class in
    ``portfolio.ASSET_CLASSES``, even one this target does not name and holds
    none of. The EDITOR passes it; a read-only view does not, because a column
    of zeros is noise where nothing can be typed. Reported: zeroing a class made
    its row disappear with no way to bring it back -- a zero DELETES the line
    (deliberately: see :func:`set_line`, and note that a line left at zero would
    rejoin the unlocked pool in :func:`apply_target_edit` and could silently be
    handed weight again), so a class held nowhere then appeared in neither
    ``lines`` nor ``current``.
    """
    target = get_target(conn, target_id) if target_id is not None else active_target(conn)
    if target is None:
        raise ValueError(
            "no allocation target to measure against; create one (or make one "
            "active) first")
    tid = int(target["id"])
    sleeve = target["sleeve"] if target["sleeve"] in TARGET_SLEEVES else "investments"
    band_abs = _D(target["band_abs_pct"], DEFAULT_BAND_ABS)
    band_rel = _D(target["band_rel_pct"], DEFAULT_BAND_REL)
    lines = target_lines(conn, tid)

    # The target's OWN accounts decide the sleeve; the stored scope is the
    # fallback for a target made before accounts could be chosen.
    chosen = target_accounts(conn, tid)
    treatment = (mixed_treatments(conn, chosen) or [""])[0] if chosen else ""
    if chosen:
        sleeve_alloc = portfolio.allocation(conn, account_ids=chosen, as_of=as_of,
                                            prices=prices)
    else:
        sleeve_alloc = portfolio.allocation(conn, as_of=as_of, prices=prices, scope=sleeve)
    stamp = target["rebalanced_on"] if "rebalanced_on" in target.keys() else None
    measured_since = stamp or since or (
        _dt.date.fromisoformat(as_of or _dt.date.today().isoformat())
        - _dt.timedelta(days=DEFAULT_SINCE_DAYS)).isoformat()
    holdings_by_class = _holdings_by_class(
        conn, sleeve_alloc, as_of=as_of, prices=prices, since=measured_since)
    total = int(sleeve_alloc.total)
    current = {s.key: int(s.value) for s in sleeve_alloc.by_class}
    sleeve_accounts = [s.label for s in sleeve_alloc.by_account]
    cash_only = list(sleeve_alloc.cash_only_accounts)

    rows: list = []
    shown = set(lines) | set(current)
    if include_empty_classes:
        shown |= set(portfolio.ASSET_CLASSES)
    for cls in sorted(shown, key=lambda c: (-current.get(c, 0), c)):
        cents = int(current.get(cls, 0))
        target_pct = lines.get(cls, Decimal("0"))
        current_pct = ((Decimal(cents) / Decimal(total)) * _HUNDRED) if total else Decimal("0")
        row = ClassDrift(
            asset_class=cls,
            label=portfolio.ASSET_CLASS_LABELS.get(cls, cls),
            current_cents=cents,
            current_pct=current_pct,
            target_pct=target_pct,
            target_cents=_cents(total, target_pct),
            out_of_band=False)
        # A sleeve worth nothing has no mix to be off; reporting every class as
        # wildly out of band would be noise, not a finding.
        row.holdings = holdings_by_class.get(cls, [])
        # The unclassified bucket is a records gap, not a position off its
        # weight: judging it against a band produced a bold red "Sell" for
        # securities whose only problem was having no asset class yet.
        row.out_of_band = bool(total) and not row.is_unclassified and not in_band(
            row.drift_pct, target_pct, band_abs, band_rel)
        rows.append(row)

    # Fixed holdings: the ASSET accounts, valued in their own right. Deliberately
    # not "everything minus the sleeve" -- that arithmetic counts a chequing
    # balance outside an investments-only sleeve as an untradeable holding, when
    # it is simply liquid money the target does not govern. Closed accounts are
    # excluded for the reason they are never valued: a sold house is not owned.
    fixed_rows: list = []
    fixed_total = 0
    # Hidden accounts are out of BOTH halves or the report contradicts itself:
    # the sleeve already excludes them (portfolio.scope_account_ids), and a
    # "not in the mix" line naming a property the user has hidden from their
    # totals would be the same phantom money in a different column.
    fixed_ids = [int(a["id"]) for a in
                 ledger.list_accounts(conn, include_closed=False, include_hidden=False)
                 if (a["type"] or "") in asset_values.VALUABLE_TYPES]
    if fixed_ids:
        fixed_alloc = portfolio.allocation(conn, account_ids=fixed_ids,
                                           as_of=as_of, prices=prices)
        fixed_total = int(fixed_alloc.total)
        fixed_rows = [(portfolio.ASSET_CLASS_LABELS.get(sl.key, sl.key), int(sl.value))
                      for sl in fixed_alloc.by_class if sl.value > 0]
        fixed_rows.sort(key=lambda pair: -pair[1])

    return DriftReport(
        target_id=tid, target_name=target["name"], sleeve=sleeve, as_of=as_of,
        sleeve_total=total, band_abs_pct=band_abs, band_rel_pct=band_rel,
        target_total_pct=sum(lines.values(), Decimal("0")),
        rows=rows, fixed_rows=fixed_rows, fixed_total=fixed_total,
        sleeve_accounts=sleeve_accounts, cash_only_accounts=cash_only,
        account_ids=chosen, tax_treatment=treatment,
        since=measured_since, since_is_rebalance=bool(stamp),
        unpriced=list(sleeve_alloc.unpriced))


def target_from_current(conn, name: str, sleeve: str = "investments",
                        as_of: Optional[str] = None, active: bool = False) -> int:
    """Create a target from the mix held TODAY, rounded to whole percent.

    The usual way anyone starts: the current mix is the one decision already
    made, and editing it beats typing seven numbers from nothing. The rounding
    remainder lands on the LARGEST class so the lines still total exactly 100 --
    a target that sums to 99 would report permanent drift nobody can clear."""
    alloc = portfolio.allocation(conn, as_of=as_of, scope=sleeve)
    total = int(alloc.total)
    if not total:
        raise ValueError("nothing in the sleeve to build a target from")
    lines: dict = {}
    for s in alloc.by_class:
        if s.key in portfolio.ASSET_CLASSES:
            lines[s.key] = (Decimal(int(s.value)) * _HUNDRED /
                            Decimal(total)).quantize(Decimal("1"),
                                                     rounding=ROUND_HALF_UP)
    lines = {k: v for k, v in lines.items() if v > 0}
    if not lines:
        raise ValueError(
            "the sleeve holds nothing classified; set each security's asset "
            "class first (Reports > Asset Allocation)")
    remainder = _HUNDRED - sum(lines.values(), Decimal("0"))
    if remainder:
        biggest = max(lines, key=lambda k: lines[k])
        lines[biggest] = lines[biggest] + remainder
    return create_target(conn, name, sleeve=sleeve, lines=lines, active=active)


def _dollars(cents: int) -> str:
    """Integer cents -> '$1,234.56'. Local on purpose: this is the DOMAIN layer,
    which must not import the UI's formatters (ui/models.fmt_cents)."""
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def describe(report: DriftReport) -> str:
    """One line for a status bar: what to do, or that there is nothing to do."""
    if not report.sleeve_total:
        return f"{SLEEVE_LABELS.get(report.sleeve, report.sleeve)} hold nothing to measure."
    if not report.needs_rebalance:
        return (f"On target: every class is inside the "
                f"{report.band_abs_pct}%/{report.band_rel_pct}% bands.")
    n = len(report.out_of_band)
    names = ", ".join(r.label for r in report.out_of_band)
    return (f"{n} class{'es' if n != 1 else ''} out of band ({names}). "
            f"Rebalancing would move {_dollars(report.to_move_cents)}.")
