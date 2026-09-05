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

**The sleeve.** A target governs the accounts it can actually be applied to.
``sleeve`` defaults to ``investments`` because a chequing balance that swings
with the month's bills manufactures drift nobody can act on, and because nobody
rebalances by selling 5% of a house. Property is reported ALONGSIDE, as context
(:attr:`DriftReport.fixed_rows`), never folded into the mix being corrected --
a drift number dominated by an illiquid position is not actionable, which is the
failure mode most tools avoid only by not knowing the house exists.

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

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from mammon import asset_values, ledger, portfolio

# The sleeves a target may govern -- a subset of portfolio.ALLOCATION_SCOPES.
# "everything" is deliberately absent: a target mix including a house is a
# target you cannot rebalance to.
TARGET_SLEEVES = ("investments", "with_cash")
SLEEVE_LABELS = {
    "investments": "Investment accounts",
    "with_cash": "Investments and cash accounts",
}
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
    return str(pct.normalize() if pct == pct.to_integral_value() else pct)


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
    if value == 0:
        conn.execute("DELETE FROM allocation_target_lines "
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
# drift
# ---------------------------------------------------------------------------
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

        For a security class it is the obvious ``buy`` (underweight) or ``sell``
        (overweight). CASH is different and must NEVER be ``sell``: you cannot
        sell cash, you spend it. It is consumed and generated only as the
        by-product of the securities trades -- when cash is OVER target the
        surplus is deployed into the underweight securities (``invest``), and
        when it is UNDER target you ``raise`` it by selling the overweight ones.
        A rebalance is therefore set up NOT to be cash-neutral on purpose: the
        cash line's move drives how far the securities trades diverge. ``hold``
        when the class already sits on its target.
        """
        if self.move_cents == 0:
            return "hold"
        if self.is_cash:
            return "invest" if self.move_cents < 0 else "raise"
        return "buy" if self.move_cents > 0 else "sell"


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
    # Sleeve accounts holding a balance but no securities (see
    # portfolio.Allocation.cash_only_accounts). They land wholly in Cash, so a
    # target's Cash line reads wildly overweight for a reason that is a data gap,
    # not a portfolio decision.
    cash_only_accounts: list = field(default_factory=list)

    @property
    def needs_rebalance(self) -> bool:
        return any(r.out_of_band for r in self.rows)

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
        trade counted once, so the buys -- which equal the sells)."""
        return sum(r.move_cents for r in self.rows if r.move_cents > 0)


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


def drift(conn, target_id: Optional[int] = None, as_of: Optional[str] = None,
          prices: Optional[dict] = None) -> DriftReport:
    """Compare the real mix against a target (the ACTIVE one when ``target_id``
    is omitted).

    Percentages are of the target's SLEEVE, not of everything owned; property and
    other fixed assets come back in ``fixed_rows`` as context. Raises when there
    is no target to measure against -- an empty report would read as "on target".
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

    sleeve_alloc = portfolio.allocation(conn, as_of=as_of, prices=prices, scope=sleeve)
    total = int(sleeve_alloc.total)
    current = {s.key: int(s.value) for s in sleeve_alloc.by_class}
    sleeve_accounts = [s.label for s in sleeve_alloc.by_account]
    cash_only = list(sleeve_alloc.cash_only_accounts)

    rows: list = []
    for cls in sorted(set(lines) | set(current),
                      key=lambda c: (-current.get(c, 0), c)):
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
        row.out_of_band = bool(total) and not in_band(
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
        sleeve_accounts=sleeve_accounts, cash_only_accounts=cash_only)


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
