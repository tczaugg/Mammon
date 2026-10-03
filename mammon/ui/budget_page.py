"""The Budget page: one month, one list, one balance (SRD 5.12;
docs/budget_one_page_design.md).

This replaces the four-tab Budget Planner. That page compared seven budgeting
applications feature by feature and took nearly everything: four tabs, a
seven-column table with three states, carried balances, committed money, a
twelve-month outlook, a cash floor, three buckets, three rollover modes, carry
overrides, three seeding bases, scenarios and groups. Each piece was defensible
alone. Together they were the learning curve the research itself named as the
leading complaint about the most demanding of those applications, and on a
real ledger it was more machinery than anyone would budget with.

Budgeting has to be exceptionally simple, because the people it helps most are
the people who have never done it. So this page is the worksheet from a
community personal-finance lesson for first-time budgeters: take-home income on
top, one list of lines underneath in the user's priority order, each marked F
(fixed: changed only by changing your situation) or V (variable: changed by
spending differently), with three columns - Planned, Spent, Remaining - and two
sentences at the bottom: does the plan balance, and how is the month going. The
acceptance test is ten minutes to a plan that balances and ten seconds a week
to read it.

Why the budget is take-home money
---------------------------------
An income line measures the paycheck's NET deposit. Withholding and premiums
are decided at open enrollment, once a year, and are not something a budget
changes; a beginner's ledger usually has no split paychecks at all. So payroll
deductions are not lines, are not spending, and never appear in "Everything
else" - the domain layer draws every actual at take-home
(:func:`mammon.budgets._month_actuals`). The retirement seam adds the measured
deductions back from the ledger, because the planner needs the whole spend
(SRD 5.12i). The three-paycheck month, which once made a withholding line read
"over", now lives where it is true: the Net pay line plans two paychecks in
most months and three in some.

Why "Everything else" is a line
-------------------------------
A coverage percentage says a plan is incomplete without saying what to do. A
last line that shows the month's spending outside the plan, and opens to the
categories behind it, says exactly what deserves a line of its own. Given a
planned amount it is the lesson's "miscellaneous" and takes part in the
balance.

Why almost everything else is under More, or gone
--------------------------------------------------
Carry, committed money, a twelve-month grid, scenarios and the retirement
handoff survive as choices under one menu; the outlook, the cash floor (which
lives on the Financial Calendar), the Analyze tab, the proposal table and the
vocabulary of buckets, flex, rollover, envelopes, groups and members do not
appear on the page at all. A line that covers several categories is a budget
group in the domain layer; the user sees a line with the categories named
beside it. The domain layer, every figure's source, is unchanged.

What this file does not do
--------------------------
No SQL, no money arithmetic beyond adding the rows the domain layer returns,
no modal on a path a test reaches: the dialogs run through one overridable
``_run_dialog`` seam and the confirmations through ``QMessageBox.question``,
the drill-down is ``show()``-n, and the Planned cells are plain items handled
by ``cellChanged`` (no delegate, so the heap bug a modal in ``setModelData``
causes is structurally impossible). Dates are entered through
``ui.delegates.make_date_edit`` and read back as ISO; money is rendered by
``ui.models.fmt_cents`` and parsed by ``parse_amount``.
"""
from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Optional

from PyQt5.QtCore import QPoint, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMenu, QMessageBox, QPushButton,
                             QRadioButton, QTableWidget, QTableWidgetItem,
                             QToolButton, QVBoxLayout, QWidget)

from decimal import Decimal, InvalidOperation

from mammon import budgets, debt, goals, ledger
from mammon.reports import listing
from mammon.ui import prefs, style
from mammon.ui.budget_basis import BudgetBasisDialog
from mammon.ui.delegates import date_edit_iso, make_date_edit, NoWheelComboBox
from mammon.ui.models import fmt_cents, fmt_date, parse_amount

#: The lesson's two kinds, one letter each, and the stored bucket behind each.
KIND_LETTER = {"fixed": "F", "flex": "V", "nonmonthly": "V"}
KIND_WORD = {"fixed": "Fixed", "flex": "Variable", "nonmonthly": "Variable"}
KIND_TOOLTIP = ("F - Fixed: a cost you change only by changing your situation "
                "(rent, insurance, a payment). Carried in the plan and reported, "
                "never graded. V - Variable: spending you change by spending "
                "differently (groceries, fuel, eating out). Graded each month. "
                "Click to switch.")

#: The ways a line can recur, in the order the dialog lists them.
EVERY_MONTH = "every month"
EVERY_TWO_WEEKS = "every two weeks from"
EVERY_WEEK = "every week from"
YEARLY_BILL = "a yearly bill of"
CERTAIN_MONTHS = "in certain months"
HOW_OFTEN = (EVERY_MONTH, EVERY_TWO_WEEKS, EVERY_WEEK, YEARLY_BILL, CERTAIN_MONTHS)

#: What a line can be for, in the order the dialog lists them.
FOR_SPENDING, FOR_INCOME, FOR_SAVING, FOR_DEBT, FOR_PAYEE = (
    "Spending", "Income", "Saving into an account", "Paying down a loan",
    "A payment, by payee")


def _month_label(period: str) -> str:
    """``"2026-03"`` -> ``"March 2026"``; a label, not a date field."""
    y, m = period.split("-")
    return f"{calendar.month_name[int(m)]} {y}"


def _short_month(period: str) -> str:
    y, m = period.split("-")
    return f"{calendar.month_abbr[int(m)]} {y}"


def _mark_negative(item: QTableWidgetItem, cents: Optional[int]) -> None:
    """Draw a negative amount in the theme's negative color, as the register
    does (a line that ran over shows red in Remaining)."""
    if cents is not None and cents < 0:
        item.setForeground(QBrush(QColor(style.negative_color())))


class _CentsItem(QTableWidgetItem):
    """A right-aligned, read-only money cell; negative amounts are red."""

    def __init__(self, cents: int, *, editable: bool = False):
        super().__init__(fmt_cents(cents))
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if editable:
            flags |= Qt.ItemIsEditable
        self.setFlags(flags)
        _mark_negative(self, cents)


# ---------------------------------------------------------------------------
# The lines (pure: reads the domain layer, does the one addition)
# ---------------------------------------------------------------------------
@dataclass
class PageLine:
    """One line of the page. ``kind`` is ``income`` | ``category`` | ``group``
    | ``account`` | ``other``; ``ident`` the category, group or account id
    (``None`` for Everything else); ``members`` the category ids its Spent
    figure drills into. Money is integer cents, positive magnitudes."""
    kind: str
    ident: Optional[int]
    label: str
    detail: str = ""
    bucket: str = "flex"
    planned_cents: int = 0
    spent_cents: int = 0
    committed_cents: int = 0
    carried_cents: int = 0
    rollover_mode: str = "none"
    members: tuple = ()
    account_type: str = ""
    #: For a payee line (SRD 5.12): the text its payments' payee contains.
    payee_match: Optional[str] = None

    @property
    def by_payee(self) -> bool:
        return bool(self.payee_match)

    @property
    def is_expense(self) -> bool:
        return self.kind != "income"

    @property
    def order_key(self) -> tuple[str, int]:
        return (self.kind if self.kind != "income" else "category", self.ident or 0)

    @property
    def can_toggle_kind(self) -> bool:
        return self.kind in ("category", "group")

    def charged(self, *, count_scheduled: bool) -> int:
        """What this month has put against the line."""
        return self.spent_cents + (self.committed_cents if count_scheduled else 0)


#: What typing in the Planned column does, said above the table, on the column
#: header and on every Planned cell: it changes the month on screen only.
PLANNED_HINT = ("Typing a Planned amount changes this month only. To change "
                "every month, right-click the line and choose Edit, or use "
                "Plan the year... under the gear.")


def _clears(sim) -> str:
    when = budgets.shift_period(sim.start_period, max(0, sim.payoff_month - 1))
    return (f"{_month_label(when)} ({sim.payoff_month} month"
            f"{'s' if sim.payoff_month != 1 else ''})")


def extra_principal_sentence(proj, *, name: str = "") -> str:
    """What a pay-down line's extra principal does (SRD 5.12h), from a
    :class:`mammon.debt.ExtraPrincipal`: when the debt clears at the regular
    payment, and how much sooner and cheaper with the extra."""
    if not proj.base.debts or proj.base.debts[0].balance_cents <= 0:
        return f"{name} owes nothing.".strip()
    d = proj.base.debts[0]
    text = (f"{name + ' owes' if name else 'Owes'} {fmt_cents(d.balance_cents)} at "
            f"{d.apr}%" + (" (estimated)" if d.estimated else "") + ".")
    base, more = proj.base, proj.with_extra
    if base.payoff_month is None:
        text += (f" At the regular {fmt_cents(proj.regular_cents)} a month it does "
                 f"not pay off.")
    else:
        text += (f" At the regular {fmt_cents(proj.regular_cents)} a month it clears "
                 f"in {_clears(base)} with {fmt_cents(base.total_interest_cents)} "
                 f"of interest.")
    if proj.extra_cents <= 0:
        return text
    if more.payoff_month is None:
        return text + (f" With {fmt_cents(proj.extra_cents)} extra principal a month "
                       f"it still does not pay off.")
    text += (f" With {fmt_cents(proj.extra_cents)} extra principal a month it clears "
             f"in {_clears(more)}")
    if proj.months_sooner is not None:
        text += (f", {proj.months_sooner} month{'s' if proj.months_sooner != 1 else ''}"
                 f" sooner and {fmt_cents(proj.interest_saved_cents)} less interest")
    return text + "."


def left_cents(line: PageLine, *, count_scheduled: bool = False) -> Optional[int]:
    """What is left on a line: planned plus carried, less spent -- negative
    when it ran over. ``None`` where there is nothing to show: an income line
    (its Spent already says what arrived) and Everything else with no plan."""
    if line.kind == "income" or (line.kind == "other" and line.planned_cents == 0):
        return None
    return (line.planned_cents + line.carried_cents
            - line.charged(count_scheduled=count_scheduled))


def left_text(line: PageLine, *, count_scheduled: bool = False) -> str:
    """The Remaining cell (``LEFT``): just the amount left, nothing else (the
    user's ruling), red when negative.

    The cell once carried words -- ``not yet``, ``as planned``, ``n above
    plan``, ``n to go``, ``incl. n carried`` -- and every one of them restated
    what the Planned and Spent numbers beside it already showed. A negative
    amount is a line that ran over; the carried amount is in the cell's hover
    (:func:`left_tooltip`)."""
    cents = left_cents(line, count_scheduled=count_scheduled)
    return "" if cents is None else fmt_cents(cents)


def left_tooltip(line: PageLine) -> str:
    """The Remaining cell's hover: what was carried in from last month, if any."""
    if not line.carried_cents or line.kind not in ("category", "group"):
        return ""
    if line.carried_cents > 0:
        return f"Includes {fmt_cents(line.carried_cents)} carried from last month."
    return (f"After {fmt_cents(-line.carried_cents)} overspent last month, "
            "carried against this one.")


def default_order(line: PageLine) -> tuple:
    """The order for a line the user has not placed: largest planned amount
    first, then by name; income in its own section, Everything else last.

    The first build ordered by kind (saving, then Fixed, then Variable), and
    switching a line's kind made it jump across the list. By amount, a line
    stays put when its kind changes, and the biggest commitments read first -
    the user's ruling. Once the user has edited the plan the order is frozen
    into ``budget_line_order`` (see ``BudgetPage._freeze_order``), so typing
    an amount never moves the row either."""
    if line.kind == "income":
        block = 0
    elif line.kind == "other":
        block = 9
    else:
        block = 1
    return (block, -line.planned_cents, line.label.lower())


def page_lines(conn, budget_id: int, period: str, *,
               paths: Optional[dict] = None) -> list[PageLine]:
    """Every line of the page for one month, in display order.

    Reads :func:`mammon.budgets.budget_vs_actual` (income rows asked for,
    members left out: a line covering several categories is one line),
    :func:`mammon.budgets.saving_vs_actual` for the save and pay-down lines,
    and :func:`mammon.budgets.other_spending` for the last line. The user's
    stored order (:func:`mammon.budgets.line_order`) places the lines it names;
    the rest fall into :func:`default_order`.
    """
    paths = paths if paths is not None else {
        c["id"]: c["path"] for c in ledger.list_categories(conn, include_hidden=True)}
    members = budgets.group_members(conn, budget_id)
    group_by_id = {g.id: g for g in budgets.list_groups(conn, budget_id)}
    accounts = {int(a["id"]): a for a in ledger.list_accounts(
        conn, include_closed=True, include_hidden=True)}
    # A line is on the plan when the budget plans for it in ANY month or the
    # user has configured it, so a month with no amount still shows the line
    # (with nothing planned) rather than dropping it into Everything else.
    on_plan = {ln.category_id for ln in budgets.get_lines(conn, budget_id)}
    on_plan |= set(budgets.get_settings(conn, budget_id))
    saving_on_plan = {sl.account_id for sl in budgets.get_saving_lines(conn, budget_id)}
    out: list[PageLine] = []
    for r in budgets.budget_vs_actual(conn, budget_id, period,
                                      include_unbudgeted=True,
                                      include_income=True):
        if not (r.is_group or r.is_income or r.category_id in on_plan):
            continue                        # spent, not planned: Everything else
        if r.is_group:
            names = [paths.get(c, "") for c in members.get(r.group_id, [])]
            group = group_by_id.get(r.group_id)
            if group is not None and group.by_payee:
                detail = f"(payments to '{group.payee_match}')"
            elif names:
                detail = "(" + ", ".join(sorted(names, key=str.lower)) + ")"
            else:
                detail = "(no categories yet)"
            out.append(PageLine(
                kind="group", ident=r.group_id, label=r.category_name,
                detail=detail, payee_match=group.payee_match if group else None,
                bucket=r.bucket, planned_cents=r.budgeted_cents,
                spent_cents=r.actual_cents, committed_cents=r.committed_cents,
                carried_cents=r.carried_in_cents, rollover_mode=r.rollover_mode,
                members=tuple(members.get(r.group_id, []))))
        else:
            out.append(PageLine(
                kind="income" if r.is_income else "category",
                ident=r.category_id, label=paths.get(r.category_id, r.category_name),
                bucket=r.bucket, planned_cents=r.budgeted_cents,
                spent_cents=r.actual_cents, committed_cents=r.committed_cents,
                carried_cents=r.carried_in_cents, rollover_mode=r.rollover_mode,
                members=(r.category_id,)))
    saving_rows = [sr for sr in budgets.saving_vs_actual(conn, budget_id, period,
                                                         include_unbudgeted=True)
                   if sr.account_id in saving_on_plan]

    def _kind(sr) -> str:
        acct = accounts.get(sr.account_id)
        return (acct["type"] or "") if acct is not None else sr.account_type

    # A pay-down line is a fixed EXTRA principal payment on top of the regular
    # one, which is budgeted where it is paid (a mortgage, one line by payee).
    # Its Spent is the extra alone -- counting the regular payment's principal
    # leg here as well counted it twice (SRD 5.12h).
    debts = [sr.account_id for sr in saving_rows if _kind(sr) == "liability"]
    extra = budgets.month_extra_principal(conn, period, debts) if debts else {}
    extra_sched = (budgets.month_extra_principal(conn, period, debts,
                                                 include_scheduled=True)
                   if debts else {})
    for sr in saving_rows:
        kind_of = _kind(sr)
        debt = kind_of == "liability"
        spent = extra.get(sr.account_id, 0) if debt else sr.actual_cents
        committed = (extra_sched.get(sr.account_id, 0) - spent if debt
                     else sr.committed_cents)
        out.append(PageLine(
            kind="account", ident=sr.account_id,
            label=sr.account_name,
            detail=f"({'extra principal' if debt else 'save into'})",
            bucket="fixed", planned_cents=sr.budgeted_cents,
            spent_cents=spent, committed_cents=committed,
            account_type=kind_of))
    other = budgets.other_spending(conn, budget_id, period)
    out.append(PageLine(
        kind="other", ident=None, label="Everything else",
        detail=(f"({len(other.by_category)} categor"
                f"{'y' if len(other.by_category) == 1 else 'ies'} not on the plan)"
                if other.by_category else ""),
        bucket="flex", planned_cents=other.planned_cents,
        spent_cents=other.actual_cents, members=other.category_ids))
    placed = budgets.line_order(conn, budget_id)
    out.sort(key=lambda ln: (
        0 if ln.kind == "income" else 1 if ln.kind != "other" else 2,
        placed.get(ln.order_key, 10_000) if ln.kind != "other" else 0,
        default_order(ln)))
    return out


def plan_sentence(lines: Iterable[PageLine]) -> str:
    """The lesson's balance: planned against take-home income."""
    lines = list(lines)
    income = sum(ln.planned_cents for ln in lines if ln.kind == "income")
    planned = sum(ln.planned_cents for ln in lines if ln.is_expense)
    has_income_line = any(ln.kind == "income" for ln in lines)
    if not has_income_line and planned == 0:
        return ("Nothing is planned yet. Start with your take-home pay: Add a "
                "line and choose Income. Or propose a plan from last year under "
                "More.")
    if not has_income_line:
        return (f"Planned {fmt_cents(planned)}. Add an income line so the plan "
                f"can balance against what you take home.")
    if planned == income:
        return f"Planned {fmt_cents(planned)} of {fmt_cents(income)} income: the plan balances."
    if planned > income:
        return (f"Planned {fmt_cents(planned)} against {fmt_cents(income)} income: "
                f"short {fmt_cents(planned - income)}.")
    return f"{fmt_cents(income - planned)} of your income is not planned yet."


def month_sentence(lines: Iterable[PageLine], period: str, today: _dt.date, *,
                   count_scheduled: bool = False) -> str:
    """The week's reading: spent so far against what you take home, and the
    days left in the month."""
    lines = list(lines)
    income = sum(ln.planned_cents for ln in lines if ln.kind == "income")
    spent = sum(ln.charged(count_scheduled=count_scheduled)
                for ln in lines if ln.is_expense)
    year, month = budgets._split_period(period)
    last = calendar.monthrange(year, month)[1]
    if (today.year, today.month) == (year, month):
        days_left = last - today.day
        when = (f"{days_left} day{'s' if days_left != 1 else ''} remaining in "
                f"{calendar.month_name[month]}")
    elif (today.year, today.month) > (year, month):
        when = f"{calendar.month_name[month]} is over"
    else:
        when = f"{calendar.month_name[month]} has not started"
    if not income and not spent:
        return ""
    if not income:
        return f"Spent {fmt_cents(spent)} so far; {when}."
    left = income - spent
    if left < 0:
        return (f"Spent {fmt_cents(spent)} so far, {fmt_cents(-left)} more than "
                f"your income; {when}.")
    return f"Spent {fmt_cents(spent)} so far; {fmt_cents(left)} left; {when}."


# ---------------------------------------------------------------------------
# Add a line
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LineChoice:
    """One thing a line can be for, with the history the dialog proposes
    from: ``samples`` per complete month, oldest first. For an account,
    ``balance_cents`` is what it holds today (a saving account) or what is
    owed on it (a debt, positive), ``apr`` the debt's rate as stored or
    synthesized and ``estimated`` whether that rate is a guess."""
    id: int
    label: str
    samples: tuple = ()
    balance_cents: int = 0
    apr: Optional[str] = None
    estimated: bool = False

    @property
    def total(self) -> int:
        return sum(self.samples)


@dataclass(frozen=True)
class AddLineRequest:
    """What the dialog collected. ``purpose`` is one of the FOR_* strings,
    ``ids`` the chosen category or account ids, ``name`` the line's name when
    several categories are covered, ``bucket`` ``fixed`` or ``flex``,
    ``amount_cents`` per month (or per occurrence, or per year for a yearly
    bill), ``how`` one of :data:`HOW_OFTEN`, ``first_date`` for the dated
    cadences and ``months`` the ISO periods for "in certain months"."""
    purpose: str
    ids: tuple
    name: str
    bucket: str
    amount_cents: int
    how: str
    first_date: Optional[str] = None
    months: tuple = ()
    payee_match: Optional[str] = None
    #: A save-into line's goal (SRD 5.12g): the target and its date, both
    #: optional. A pay-down line's APR as typed (SRD 5.12h), or None to keep
    #: the stored or synthesized rate.
    goal_cents: Optional[int] = None
    goal_date: Optional[str] = None
    apr: Optional[str] = None


class AddLineDialog(QDialog):
    """Add one line: what it is for, which categories or account, Fixed or
    Variable, how much, and how often (SRD 5.12).

    Everything is proposed and nothing is required beyond a non-zero amount.
    Choosing a category fills the amount from the last twelve complete months
    and says in one sentence what that rests on; a category an active schedule
    charges is proposed at the schedule's amount and cadence and as Fixed; an
    income category with a scheduled deposit is proposed at the net deposit
    and its cadence, which is how a Net pay line plans three paychecks in the
    months that have them. A typed amount is never overwritten by a later
    proposal. Collect-only: :meth:`result` hands back an
    :class:`AddLineRequest` and the page writes it through
    :mod:`mammon.budgets`."""

    def __init__(self, *, history, categories: list, income_categories: list,
                 accounts: list, debts: list, bill_schedules: dict,
                 income_schedules: dict, periods: list, income_note: str = "",
                 payee_samples=None, project_debt=None, today=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a line")
        self._history = history
        self._income_note = income_note
        #: ``(account id, monthly cents, apr text or None) -> Simulation`` for a
        #: pay-down line's projection; a callable, so the dialog stays free of a
        #: connection. The page supplies :func:`mammon.debt.project_debt`.
        self._project_debt = project_debt
        self._today = today or _dt.date.today()
        #: ``match -> per-month whole payments`` over the history periods, for a
        #: payee line's proposal. A callable so the dialog stays free of SQL and
        #: of a connection; the page supplies :func:`mammon.budgets.payee_history`.
        self._payee_samples = payee_samples or (lambda match: [])
        self._choices = {FOR_SPENDING: list(categories),
                         FOR_INCOME: list(income_categories),
                         FOR_SAVING: list(accounts), FOR_DEBT: list(debts),
                         FOR_PAYEE: []}
        self._bills = dict(bill_schedules)
        self._incomes = dict(income_schedules)
        self._periods = list(periods)
        self._amount_touched = False
        self._build()

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 10, 10, 10)
        box.setSpacing(6)

        def row(label: str, widget: QWidget) -> QLabel:
            line = QHBoxLayout()
            caption = QLabel(label)
            caption.setMinimumWidth(120)
            line.addWidget(caption)
            line.addWidget(widget, 1)
            box.addLayout(line)
            return caption

        self.purpose_combo = NoWheelComboBox()
        self.purpose_combo.addItems([FOR_SPENDING, FOR_INCOME, FOR_SAVING, FOR_DEBT,
                                     FOR_PAYEE])
        self.purpose_combo.setToolTip(
            "A payment, by payee: one line for a payment you want counted whole "
            "whatever it is split into - a mortgage payment with its principal, "
            "interest and escrow - matched by the payee's name.")
        row("This line is for:", self.purpose_combo)

        self.payee_edit = QLineEdit()
        self.payee_edit.setPlaceholderText("part of the payee's name, as it appears in the register")
        self.payee_caption = row("Payee contains:", self.payee_edit)

        self.choice_list = QListWidget()
        self.choice_list.setMinimumHeight(180)
        self.choice_list.setMinimumWidth(380)
        box.addWidget(self.choice_list, 1)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Food")
        self.name_caption = row("Call it:", self.name_edit)

        kinds = QHBoxLayout()
        self.variable_radio = QRadioButton("Variable - spending I can change")
        self.fixed_radio = QRadioButton("Fixed - a cost I carry")
        self.variable_radio.setChecked(True)
        self.variable_radio.setToolTip(KIND_TOOLTIP)
        self.fixed_radio.setToolTip(KIND_TOOLTIP)
        kinds.addWidget(self.variable_radio)
        kinds.addWidget(self.fixed_radio)
        kinds.addStretch(1)
        self.kind_widget = QWidget()
        self.kind_widget.setLayout(kinds)
        self.kind_caption = row("Kind:", self.kind_widget)

        self.amount_edit = QLineEdit()
        self.amount_edit.setAlignment(Qt.AlignRight)
        self.amount_caption = row("Amount a month:", self.amount_edit)

        self.how_combo = NoWheelComboBox()
        self.how_combo.addItems(list(HOW_OFTEN))
        row("How often:", self.how_combo)
        # A save-into line's goal: a target and a date, both optional. Set,
        # the required amount per month is proposed and the hover on the line
        # reports whether the plan gets there (SRD 5.12g).
        self.goal_edit = QLineEdit()
        self.goal_edit.setAlignment(Qt.AlignRight)
        self.goal_edit.setPlaceholderText("optional")
        self.goal_caption = row("Save up to:", self.goal_edit)
        self.goal_date_edit = make_date_edit(self, blank_ok=True)
        self.goal_date_caption = row("by:", self.goal_date_edit)
        # A pay-down line's rate: the stored or synthesized APR, editable, so
        # the projection beneath says what the typed payment does (SRD 5.12h).
        self.apr_edit = QLineEdit()
        self.apr_edit.setAlignment(Qt.AlignRight)
        self.apr_edit.setPlaceholderText("APR, percent")
        self.apr_caption = row("Interest rate:", self.apr_edit)
        self.first_date_edit = make_date_edit(
            self, iso=f"{self._periods[0]}-01" if self._periods else "")
        self.first_date_caption = row("First date:", self.first_date_edit)
        self.months_widget = QWidget()
        grid = QGridLayout(self.months_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        self.month_boxes: list[QCheckBox] = []
        for i, period in enumerate(self._periods):
            cb = QCheckBox(_short_month(period))
            cb.setProperty("period", period)
            grid.addWidget(cb, i // 4, i % 4)
            self.month_boxes.append(cb)
        self.months_caption = row("Which months:", self.months_widget)

        self.basis_label = QLabel("")
        self.basis_label.setWordWrap(True)
        box.addWidget(self.basis_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        box.addWidget(self.buttons)

        self.purpose_combo.currentIndexChanged.connect(self._purpose_changed)
        self.payee_edit.textChanged.connect(self._payee_changed)
        self.choice_list.itemChanged.connect(self._selection_changed)
        self.choice_list.itemSelectionChanged.connect(self._selection_changed)
        self.how_combo.currentIndexChanged.connect(self._how_changed)
        self.amount_edit.textEdited.connect(self._amount_typed)
        self.amount_edit.textChanged.connect(self._update_ok)
        self.name_edit.textChanged.connect(self._update_ok)
        self.first_date_edit.dateChanged.connect(self._refresh_basis)
        for cb in self.month_boxes:
            cb.toggled.connect(self._update_ok)
        self.goal_edit.textChanged.connect(self._goal_changed)
        self.goal_date_edit.dateChanged.connect(self._goal_changed)
        self.apr_edit.textChanged.connect(self._refresh_basis)
        self.amount_edit.textChanged.connect(self._amount_changed_for_debt)
        self._purpose_changed()

    # -- state ----------------------------------------------------------------
    @property
    def purpose(self) -> str:
        return self.purpose_combo.currentText()

    @property
    def how(self) -> str:
        return self.how_combo.currentText()

    @property
    def multi(self) -> bool:
        """Whether several categories may be chosen: spending only, where the
        several become one line that covers them."""
        return self.purpose == FOR_SPENDING

    @property
    def by_payee(self) -> bool:
        return self.purpose == FOR_PAYEE

    def payee_text(self) -> str:
        return self.payee_edit.text().strip()

    def chosen(self) -> list[LineChoice]:
        out = []
        for i in range(self.choice_list.count()):
            item = self.choice_list.item(i)
            if self.multi:
                if item.checkState() == Qt.Checked:
                    out.append(item.data(Qt.UserRole))
            elif item.isSelected():
                out.append(item.data(Qt.UserRole))
        return out

    def choose(self, ids: Iterable[int]) -> None:
        """Select exactly these ids. Public so a test (and the row menu's
        "change" path) can fill the list."""
        wanted = {int(i) for i in ids}
        for i in range(self.choice_list.count()):
            item = self.choice_list.item(i)
            hit = item.data(Qt.UserRole).id in wanted
            if self.multi:
                item.setCheckState(Qt.Checked if hit else Qt.Unchecked)
            else:
                item.setSelected(hit)

    def first_date(self) -> str:
        return date_edit_iso(self.first_date_edit)

    def chosen_months(self) -> tuple:
        return tuple(cb.property("period") for cb in self.month_boxes if cb.isChecked())

    # -- behavior -----------------------------------------------------------
    def _purpose_changed(self, *_args) -> None:
        self.choice_list.blockSignals(True)
        try:
            self.choice_list.clear()
            self.choice_list.setSelectionMode(
                QAbstractItemView.NoSelection if self.multi
                else QAbstractItemView.SingleSelection)
            for c in self._choices[self.purpose]:
                mean = budgets.mean_cents(c.total, max(len(c.samples), 1))
                text = c.label
                if mean > 0:
                    text += f"     {fmt_cents(mean)} a month"
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, c)
                if self.multi:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Unchecked)
                else:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.choice_list.addItem(item)
        finally:
            self.choice_list.blockSignals(False)
        spending = self.purpose in (FOR_SPENDING, FOR_PAYEE)
        self.kind_widget.setVisible(spending)
        self.kind_caption.setVisible(spending)
        self.choice_list.setVisible(not self.by_payee)
        self.payee_edit.setVisible(self.by_payee)
        self.payee_caption.setVisible(self.by_payee)
        saving = self.purpose == FOR_SAVING
        for w in (self.goal_edit, self.goal_caption, self.goal_date_edit,
                  self.goal_date_caption):
            w.setVisible(saving)
        debt = self.purpose == FOR_DEBT
        self.apr_edit.setVisible(debt)
        self.apr_caption.setVisible(debt)
        self._how_changed_visibility()          # the amount reads "Extra principal"
        if self.by_payee:
            # A payment counted whole is a cost the household carries.
            self.fixed_radio.setChecked(True)
        self._amount_touched = False
        self._selection_changed()

    def _payee_changed(self, _text: str) -> None:
        if self.by_payee:
            self._selection_changed()

    # -- a goal on a save-into line -------------------------------------------
    def goal_cents(self) -> Optional[int]:
        text = self.goal_edit.text().strip()
        if self.purpose != FOR_SAVING or not text:
            return None
        cents = parse_amount(text)
        return cents if cents > 0 else None

    def goal_date(self) -> Optional[str]:
        if self.purpose != FOR_SAVING:
            return None
        return date_edit_iso(self.goal_date_edit) or None

    def _goal_changed(self, *_args) -> None:
        if self.purpose != FOR_SAVING:
            return
        chosen = self.chosen()
        target, when = self.goal_cents(), self.goal_date()
        if target and when and not self._amount_touched and chosen:
            months = max(1, self._months_through(when))
            remaining = max(0, target)      # new money only: the baseline is today
            required = -(-remaining // months)
            self.amount_edit.setText(fmt_cents(required))
        self._refresh_basis()

    def _months_through(self, iso: str) -> int:
        """Months from this month THROUGH the month of ``iso``, inclusive."""
        y, m = int(iso[:4]), int(iso[5:7])
        return (y - self._today.year) * 12 + (m - self._today.month) + 1

    def _goal_sentence(self, chosen: list) -> str:
        target, when = self.goal_cents(), self.goal_date()
        if not chosen or not target:
            return ""
        c = chosen[0]
        head = f" {c.label} holds {fmt_cents(c.balance_cents)} today."
        if not when:
            return head + (f" Saving up to {fmt_cents(target)} from here counts only "
                           f"money saved from now; without a date there is no "
                           f"required amount per month.")
        months = max(1, self._months_through(when))
        required = -(-target // months)
        return head + (f" Reaching {fmt_cents(target)} by {fmt_date(when)} takes "
                       f"{fmt_cents(required)} a month over {months} month"
                       f"{'s' if months != 1 else ''}, counting only money saved "
                       f"from now.")

    # -- a payoff projection on a pay-down line --------------------------------
    def apr_text(self) -> Optional[str]:
        text = self.apr_edit.text().strip().rstrip("%").strip()
        return text or None

    def _amount_changed_for_debt(self, *_args) -> None:
        if self.purpose == FOR_DEBT:
            self._refresh_basis()

    def _debt_sentence(self, chosen: list) -> str:
        """What the typed EXTRA principal does to the chosen debt (SRD 5.12h):
        the regular payment is budgeted where it is paid, so the line's amount
        is only the extra on top of it."""
        if not chosen or self._project_debt is None:
            return ""
        c = chosen[0]
        try:
            proj = self._project_debt(c.id, max(0, parse_amount(self.amount_edit.text())),
                                      self.apr_text())
        except (ValueError, KeyError, ArithmeticError):
            return ""
        text = " " + extra_principal_sentence(proj, name=c.label)
        if c.estimated and not self.apr_text():
            text += " The rate is estimated; type the loan's APR."
        if parse_amount(self.amount_edit.text()) <= 0:
            text += " Type an extra principal amount to see what it saves."
        return text

    def _selection_changed(self, *_args) -> None:
        if self.by_payee:
            self.name_edit.setVisible(True)
            self.name_caption.setVisible(True)
            self._propose_payee()
            self._update_ok()
            return
        chosen = self.chosen()
        several = len(chosen) > 1
        self.name_edit.setVisible(several)
        self.name_caption.setVisible(several)
        self._propose(chosen)
        self._update_ok()

    def _propose_payee(self) -> None:
        """A payee line's amount from the matching payments of the last twelve
        complete months, counted whole; the name defaults to the text typed."""
        text = self.payee_text()
        if not text:
            self._basis_text = ("Type part of the payee's name. Every payment to it "
                                "out of your spending accounts is counted whole - a "
                                "mortgage payment with its principal, interest and "
                                "escrow as one figure - and those payments leave the "
                                "category lines so nothing is counted twice.")
            self.basis_label.setText(self._basis_text)
            return
        samples = list(self._payee_samples(text))
        summary = budgets.HistorySamples.summary(samples)
        if not self._amount_touched:
            amount = (summary["mean"] if self.how in (EVERY_MONTH, CERTAIN_MONTHS)
                      else summary["total"] if self.how == YEARLY_BILL
                      else budgets.mean_cents(
                          summary["total"] * 12,
                          max(len(samples), 1) * budgets.DATED_PER_YEAR[
                              "biweekly" if self.how == EVERY_TWO_WEEKS else "weekly"]))
            self.amount_edit.setText(fmt_cents(amount) if amount > 0 else "")
        if not self.name_edit.text().strip() or self.name_edit.property("auto"):
            self.name_edit.setText(text.title() if text.islower() else text)
            self.name_edit.setProperty("auto", True)
        months = sum(1 for v in samples if v)
        if summary["total"] <= 0:
            self._basis_text = (f"No payment to a payee containing '{text}' in the "
                                f"last 12 complete months.")
        else:
            self._basis_text = (f"Payments to '{text}' in the last 12 months: "
                                f"{fmt_cents(summary['total'])} in all over {months} "
                                f"month{'s' if months != 1 else ''}, "
                                f"{fmt_cents(summary['mean'])} a month, counted whole.")
        self._refresh_basis()

    def _how_changed(self, *_args) -> None:
        self._how_changed_visibility()
        if self.by_payee:
            self._propose_payee()
        elif not self._amount_touched:
            self._propose(self.chosen(), keep_how=True)
        else:
            self._refresh_basis()
        self._update_ok()

    def _amount_typed(self, _text: str) -> None:
        self._amount_touched = True

    def _update_ok(self, *_args) -> None:
        if self.by_payee:
            ok = (bool(self.payee_text()) and bool(self.name_edit.text().strip())
                  and parse_amount(self.amount_edit.text()) != 0
                  and (self.how != CERTAIN_MONTHS or bool(self.chosen_months())))
        else:
            chosen = self.chosen()
            ok = (bool(chosen) and parse_amount(self.amount_edit.text()) != 0
                  and (len(chosen) == 1 or bool(self.name_edit.text().strip()))
                  and (self.how != CERTAIN_MONTHS or bool(self.chosen_months())))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def _propose(self, chosen: list, *, keep_how: bool = False) -> None:
        """Fill the amount, the cadence and the kind from history and the
        schedules, but never over an amount the user typed."""
        if not chosen:
            if self.purpose == FOR_INCOME and self._income_note:
                self._basis_text = self._income_note
            elif self.purpose == FOR_INCOME:
                self._basis_text = ("Pick the category your pay is entered under. "
                                    "The amount is what you take home.")
            else:
                self._basis_text = "Pick what this line is for."
            self.basis_label.setText(self._basis_text)
            return
        samples = [sum(c.samples[i] for c in chosen if i < len(c.samples))
                   for i in range(len(self._history.periods))]
        summary = budgets.HistorySamples.summary(samples)
        how, amount, fixed = EVERY_MONTH, summary["mean"], False
        schedules = []
        if self.purpose == FOR_SPENDING:
            for c in chosen:
                schedules.extend(self._bills.get(c.id, []))
        elif self.purpose == FOR_INCOME:
            for c in chosen:
                schedules.extend(self._incomes.get(c.id, []))
        if len(chosen) == 1 and len(schedules) == 1:
            d = schedules[0]
            cents = d.get("per_occurrence_cents", d.get("net_cents", 0))
            freq = d["frequency"]
            fixed = self.purpose == FOR_SPENDING
            if freq == "biweekly":
                how, amount = EVERY_TWO_WEEKS, cents
            elif freq == "weekly":
                how, amount = EVERY_WEEK, cents
            elif freq in ("quarterly", "semiannual", "annual", "yearly"):
                how = YEARLY_BILL
                amount = cents * {"quarterly": 4, "semiannual": 2}.get(freq, 1)
            else:
                how, amount = EVERY_MONTH, cents
            if how in (EVERY_TWO_WEEKS, EVERY_WEEK):
                from PyQt5.QtCore import QDate
                self.first_date_edit.setDate(
                    QDate.fromString(d["next_date"], "yyyy-MM-dd"))
        if not keep_how:
            self.how_combo.blockSignals(True)
            try:
                self.how_combo.setCurrentText(how)
            finally:
                self.how_combo.blockSignals(False)
            self._how_changed_visibility()
            if self.purpose == FOR_SPENDING:
                (self.fixed_radio if fixed else self.variable_radio).setChecked(True)
        else:
            if self.how in (EVERY_TWO_WEEKS, EVERY_WEEK):
                per_year = budgets.DATED_PER_YEAR["biweekly" if self.how == EVERY_TWO_WEEKS
                                                  else "weekly"]
                amount = budgets.mean_cents(summary["total"] * 12,
                                            max(len(samples), 1) * per_year)
            elif self.how == YEARLY_BILL:
                amount = summary["total"]
            else:
                amount = summary["mean"]
        if self.purpose == FOR_DEBT and len(chosen) == 1:
            c = chosen[0]
            if not self.apr_edit.text().strip() or getattr(self, "_apr_for", None) != c.id:
                self.apr_edit.blockSignals(True)
                try:
                    self.apr_edit.setText(c.apr or "")
                finally:
                    self.apr_edit.blockSignals(False)
                self._apr_for = c.id
        if not self._amount_touched:
            self.amount_edit.setText(fmt_cents(amount) if amount > 0 else "")
        self._basis_text = self._history_sentence(summary, schedules)
        self._refresh_basis()

    def _how_changed_visibility(self) -> None:
        dated = self.how in (EVERY_TWO_WEEKS, EVERY_WEEK)
        self.first_date_edit.setVisible(dated)
        self.first_date_caption.setVisible(dated)
        certain = self.how == CERTAIN_MONTHS
        self.months_widget.setVisible(certain)
        self.months_caption.setVisible(certain)
        self.amount_caption.setText(
            "Extra principal a month:" if self.purpose == FOR_DEBT and not (
                dated or certain or self.how == YEARLY_BILL) else
            "Amount each time:" if dated else
            "Yearly amount:" if self.how == YEARLY_BILL else
            "Amount each month:" if certain else "Amount a month:")

    def _history_sentence(self, summary: dict, schedules: list) -> str:
        if schedules:
            parts = [f"'{d['payee']}' {d['frequency']}, "
                     f"{fmt_cents(d.get('per_occurrence_cents', d.get('net_cents', 0)))} "
                     f"each" for d in schedules]
            head = "Scheduled: " + "; ".join(parts) + "."
        elif summary["total"] <= 0:
            head = "Nothing in the last 12 complete months to propose from."
        else:
            periods = self._history.periods
            high_month = (_short_month(periods[summary["high_index"]])
                          if periods else "")
            head = (f"Last 12 months: {fmt_cents(summary['total'])} in all, "
                    f"{fmt_cents(summary['mean'])} a month, highest "
                    f"{fmt_cents(summary['high'])} in {high_month}.")
            if summary["volatile"]:
                head += (f" This moves a lot month to month; {fmt_cents(summary['mean'])} "
                         f"is the average and half the months were under "
                         f"{fmt_cents(summary['median'])}.")
        return head

    def _refresh_basis(self, *_args) -> None:
        text = getattr(self, "_basis_text", "")
        if self.how in (EVERY_TWO_WEEKS, EVERY_WEEK) and self._periods:
            freq = "biweekly" if self.how == EVERY_TWO_WEEKS else "weekly"
            try:
                counts = budgets.dated_frequency_counts(
                    self._periods[0], self.first_date(), freq,
                    months=len(self._periods))
            except ValueError:
                counts = {}
            if counts:
                by_count: dict[int, int] = {}
                for n in counts.values():
                    by_count[n] = by_count.get(n, 0) + 1
                parts = [f"{m} month{'s' if m != 1 else ''} with {n}"
                         for n, m in sorted(by_count.items())]
                text += (f" From {fmt_date(self.first_date())}: {sum(counts.values())} "
                         f"times in the plan, {', '.join(parts)}; each month gets "
                         f"its times the amount.")
        elif self.how == YEARLY_BILL:
            text += (" Spread evenly over the twelve months as a set-aside that "
                     "carries forward until the bill lands.")
        if self.purpose == FOR_SAVING:
            text += self._goal_sentence(self.chosen())
        elif self.purpose == FOR_DEBT:
            text += self._debt_sentence(self.chosen())
        self.basis_label.setText(text)

    def result(self) -> Optional[AddLineRequest]:
        dated = self.how in (EVERY_TWO_WEEKS, EVERY_WEEK)
        if self.by_payee:
            if not self.payee_text() or not self.name_edit.text().strip():
                return None
            return AddLineRequest(
                purpose=FOR_PAYEE, ids=(), name=self.name_edit.text().strip(),
                bucket="fixed" if self.fixed_radio.isChecked() else "flex",
                amount_cents=parse_amount(self.amount_edit.text()), how=self.how,
                first_date=self.first_date() if dated else None,
                months=self.chosen_months() if self.how == CERTAIN_MONTHS else (),
                payee_match=self.payee_text())
        chosen = self.chosen()
        if not chosen:
            return None
        if self.purpose == FOR_SPENDING:
            bucket = "fixed" if self.fixed_radio.isChecked() else "flex"
        elif self.purpose == FOR_INCOME:
            bucket = "income"
        else:
            bucket = "fixed"
        return AddLineRequest(
            purpose=self.purpose, ids=tuple(c.id for c in chosen),
            name=self.name_edit.text().strip() if len(chosen) > 1 else chosen[0].label,
            bucket=bucket, amount_cents=parse_amount(self.amount_edit.text()),
            how=self.how, first_date=self.first_date() if dated else None,
            months=self.chosen_months() if self.how == CERTAIN_MONTHS else (),
            goal_cents=self.goal_cents(), goal_date=self.goal_date(),
            apr=self.apr_text() if self.purpose == FOR_DEBT else None)


# ---------------------------------------------------------------------------
# Drill-down (read-only, shown, never exec_()-ed)
# ---------------------------------------------------------------------------
def spent_rows(conn, ln: PageLine, start: str, end: str) -> list[tuple]:
    """``(date, payee, cents)`` behind a line's Spent for ``[start, end]``,
    newest first: the matched payments for a payee line, the transfer legs for
    a save or pay-down line, the deposits for an income line, otherwise the
    listing report over the line's categories (uncategorized rows too for
    Everything else). One fetcher serves the hover and the drill-down so the
    two can never disagree."""
    accts = budgets.spending_account_ids(conn)
    rows: list[tuple] = []
    if ln.by_payee:
        found = budgets.payee_payments(conn, start, end, ln.payee_match or "")
        rows = [(d, payee, cents) for d, payee, cents, _i, _a in found.payments]
    elif ln.kind == "account" and ln.account_type == "liability":
        # A pay-down line counts extra principal only (SRD 5.12h).
        from mammon import loans
        rows = loans.extra_principal_items(conn, ln.ident, start, end)
    elif ln.kind == "account":
        from mammon.reports.saving import saving_lines
        rows = [(x.date, x.payee, -int(x.amount)) for x in saving_lines(conn, start, end)
                if int(x.transfer_account_id) == ln.ident]
    elif ln.kind == "income":
        report = listing.transactions(conn, start, end, account_ids=accts,
                                      category_ids=list(ln.members),
                                      expand_subtree=False, include_transfers=False,
                                      include_scheduled=False)
        rows = [(r["date"], r["payee"], int(r["amount"])) for r in report.rows
                if int(r["amount"]) > 0]
    else:
        if ln.members:
            report = listing.transactions(conn, start, end, account_ids=accts,
                                          category_ids=list(ln.members),
                                          expand_subtree=False, include_transfers=False,
                                          include_scheduled=False)
            rows = [(r["date"], r["payee"], -int(r["amount"])) for r in report.rows
                    if int(r["amount"]) < 0]
        if ln.kind == "other":
            seen_dates = {(d, p, c) for d, p, c in rows}
            everything = listing.transactions(conn, start, end, account_ids=accts,
                                              include_transfers=False,
                                              include_scheduled=False)
            for r in everything.rows:
                if int(r["amount"]) < 0 and not r["category"]:
                    row = (r["date"], r["payee"], -int(r["amount"]))
                    if row not in seen_dates:
                        rows.append(row)
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


class LineDrillDownDialog(QDialog):
    """The transactions behind one line's Spent figure for the month, re-read
    through the ordinary listing report so the rows are the register's. For
    Everything else the list is grouped by category, biggest first, which is
    how the user decides what deserves a line of its own."""

    HEADERS = ("Date", "Account", "Payee", "Category", "Amount")

    def __init__(self, conn, *, title: str, start: str, end: str,
                 category_ids: Iterable[int], account_ids: Iterable[int],
                 include_uncategorized: bool = False,
                 payee_match: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle(title)
        self.resize(720, 420)
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 8)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        box.addWidget(self.table, 1)
        self.footer = QLabel("")
        self.footer.setWordWrap(True)
        box.addWidget(self.footer)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        cats = [int(c) for c in category_ids]
        accts = [int(a) for a in account_ids]
        rows: list = []
        if payee_match:
            # A payee line: its payments counted whole, whatever their legs.
            found = budgets.payee_payments(conn, start, end, payee_match)
            names = {int(a["id"]): a["name"] for a in ledger.list_accounts(
                conn, include_closed=True, include_hidden=True)}
            rows = [{"id": i, "date": d, "account": names.get(acct, ""), "payee": p,
                     "category": "(whole payment)", "amount": -c}
                    for d, p, c, i, acct in found.payments]
            cats = []
        if cats:
            report = listing.transactions(
                conn, start, end, account_ids=accts, category_ids=cats,
                expand_subtree=False, include_transfers=False,
                include_scheduled=False)
            rows.extend(r for r in report.rows if int(r["amount"]) < 0)
        if include_uncategorized:
            # Rows with no category at all: the listing's category filter can
            # never match them, so they are asked for without one.
            seen = {r["id"] for r in rows}
            everything = listing.transactions(
                conn, start, end, account_ids=accts, include_transfers=False,
                include_scheduled=False)
            rows.extend(r for r in everything.rows
                        if int(r["amount"]) < 0 and not r["category"]
                        and r["id"] not in seen)
        rows.sort(key=lambda r: ((r["category"] or "").lower(), r["date"]))
        self.table.setRowCount(len(rows))
        total = 0
        for i, r in enumerate(rows):
            total += -int(r["amount"])
            self.table.setItem(i, 0, QTableWidgetItem(fmt_date(r["date"])))
            self.table.setItem(i, 1, QTableWidgetItem(r["account"] or ""))
            self.table.setItem(i, 2, QTableWidgetItem(r["payee"] or ""))
            self.table.setItem(i, 3, QTableWidgetItem(r["category"] or "Uncategorized"))
            self.table.setItem(i, 4, _CentsItem(int(r["amount"])))
        self.footer.setText(
            f"{len(rows)} transactions, {fmt_cents(total)}, {fmt_date(start)} to "
            f"{fmt_date(end)}. Read-only; change a transaction in its register.")


# ---------------------------------------------------------------------------
# Plan the year
# ---------------------------------------------------------------------------
class PlanYearDialog(QDialog):
    """The same lines across the budget's twelve months, Planned only: a
    second view of the plan for someone who wants to see the whole year, not a
    second place where decisions live. Every cell is typed into directly and
    an emptied cell removes that month's amount; the writes are the same
    domain functions the page uses."""

    LABEL = 0
    MONTH0 = 1

    def __init__(self, conn, budget_id: int, *, lines: list, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.budget_id = budget_id
        self._lines = [ln for ln in lines]
        budget = budgets.get_budget(conn, budget_id)
        self._periods = budgets.plan_periods(budget.start_period) if budget else []
        self.setWindowTitle(f"Plan the year - {budget.name if budget else ''}")
        self.resize(1100, 480)
        self._filling = False
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 8)
        self.table = QTableWidget(len(self._lines), 1 + len(self._periods))
        self.table.setHorizontalHeaderLabels(
            ["Line"] + [_short_month(p) for p in self._periods])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(self.LABEL, QHeaderView.Stretch)
        for col in range(self.MONTH0, 1 + len(self._periods)):
            head.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        box.addWidget(self.table, 1)
        self.hint = QLabel("Type in any month; empty a month to remove its amount. "
                           "Everything else takes a planned amount too.")
        self.hint.setWordWrap(True)
        box.addWidget(self.hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        box.addWidget(buttons)
        self.table.cellChanged.connect(self._cell_changed)
        self._fill()

    def amounts_for(self, line: PageLine) -> dict[str, int]:
        bid = self.budget_id
        if line.kind in ("category", "income"):
            return {ln.period: ln.amount_cents for ln in budgets.get_lines(self.conn, bid)
                    if ln.category_id == line.ident}
        if line.kind == "group":
            return {gl.period: gl.amount_cents
                    for gl in budgets.get_group_lines(self.conn, bid)
                    if gl.group_id == line.ident}
        if line.kind == "account":
            return {sl.period: sl.amount_cents
                    for sl in budgets.get_saving_lines(self.conn, bid)
                    if sl.account_id == line.ident}
        return budgets.get_other_lines(self.conn, bid)

    def _fill(self) -> None:
        self._filling = True
        try:
            for r, line in enumerate(self._lines):
                name = QTableWidgetItem(line.label + (f"  {line.detail}" if line.detail else ""))
                name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(r, self.LABEL, name)
                amounts = self.amounts_for(line)
                for i, period in enumerate(self._periods):
                    cents = amounts.get(period)
                    item = QTableWidgetItem("" if cents is None else fmt_cents(cents))
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(r, self.MONTH0 + i, item)
        finally:
            self._filling = False

    def _cell_changed(self, row: int, col: int) -> None:
        if self._filling or col < self.MONTH0 or not (0 <= row < len(self._lines)):
            return
        line = self._lines[row]
        period = self._periods[col - self.MONTH0]
        item = self.table.item(row, col)
        text = (item.text() if item is not None else "").strip()
        bid = self.budget_id
        write_line(self.conn, bid, line, period,
                   parse_amount(text) if text else None)
        amounts = self.amounts_for(line)
        self._filling = True
        try:
            cents = amounts.get(period)
            item.setText("" if cents is None else fmt_cents(cents))
        finally:
            self._filling = False


def write_line(conn, budget_id: int, line: PageLine, period: str,
               cents: Optional[int]) -> None:
    """One month's planned amount for a line, or (``None``) its removal,
    through the domain function for the line's kind. The one place the page
    maps a line kind to a writer, shared by the grid, the year view and the
    row menu."""
    if line.kind in ("category", "income"):
        if cents is None:
            budgets.clear_line(conn, budget_id, line.ident, period)
        else:
            budgets.set_line(conn, budget_id, line.ident, period, cents)
    elif line.kind == "group":
        if cents is None:
            budgets.clear_group_line(conn, budget_id, line.ident, period)
        else:
            budgets.set_group_line(conn, budget_id, line.ident, period, cents)
    elif line.kind == "account":
        if cents is None:
            budgets.delete_saving_line(conn, budget_id, line.ident, period)
        else:
            budgets.set_saving_line(conn, budget_id, line.ident, period, cents)
    else:
        if cents is None or cents == 0:
            budgets.clear_other_line(conn, budget_id, period)
        else:
            budgets.set_other_line(conn, budget_id, period, cents)


# ---------------------------------------------------------------------------
# Accounts and categories: what the budget covers
# ---------------------------------------------------------------------------
class BudgetScopeDialog(QDialog):
    """Which spending accounts the budget watches, and which categories it
    leaves out altogether (SRD 5.12, schema v114). A household ledger with a
    business checking account and business categories is the case: that
    spending is real but is not the household's plan, and without this it
    would land in "Everything else". Collect-only; :meth:`result` hands back
    ``(account ids, excluded category ids)``."""

    def __init__(self, *, accounts: list, chosen: Iterable[int],
                 categories: list, excluded: Iterable[int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Accounts and categories")
        self.resize(560, 520)
        chosen = {int(a) for a in chosen}
        excluded = {int(c) for c in excluded}
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 10, 10, 10)
        box.addWidget(QLabel("Accounts this budget covers (money spent from these "
                             "is the budget's spending; take-home received into "
                             "them is its income):"))
        self.accounts_list = QListWidget()
        for aid, name in accounts:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, int(aid))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if int(aid) in chosen else Qt.Unchecked)
            self.accounts_list.addItem(item)
        box.addWidget(self.accounts_list, 1)
        box.addWidget(QLabel("Categories to leave out entirely (never a line, never "
                             "in Everything else):"))
        self.categories_list = QListWidget()
        for cid, path in categories:
            item = QListWidgetItem(path)
            item.setData(Qt.UserRole, int(cid))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if int(cid) in excluded else Qt.Unchecked)
            self.categories_list.addItem(item)
        box.addWidget(self.categories_list, 2)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        box.addWidget(self.buttons)

    @staticmethod
    def _checked(widget: QListWidget) -> list[int]:
        return [int(widget.item(i).data(Qt.UserRole)) for i in range(widget.count())
                if widget.item(i).checkState() == Qt.Checked]

    def set_accounts(self, ids: Iterable[int]) -> None:
        wanted = {int(a) for a in ids}
        for i in range(self.accounts_list.count()):
            item = self.accounts_list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in wanted
                               else Qt.Unchecked)

    def set_excluded(self, ids: Iterable[int]) -> None:
        wanted = {int(c) for c in ids}
        for i in range(self.categories_list.count()):
            item = self.categories_list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in wanted
                               else Qt.Unchecked)

    def result(self) -> tuple[list[int], list[int]]:
        return self._checked(self.accounts_list), self._checked(self.categories_list)


# ---------------------------------------------------------------------------
# Budgets... (rename, copy, start month, delete, make active)
# ---------------------------------------------------------------------------
class BudgetsDialog(QDialog):
    """The once-a-year chores: a new plan, a copy of this one for next year,
    a rename, the start month, which plan is active, and deletion. Each write
    is a domain function; the dialog reports and returns."""

    def __init__(self, conn, *, today: _dt.date, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._today = today
        self.setWindowTitle("Budgets")
        self.resize(520, 360)
        self._ids: list[int] = []
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 10, 10, 10)
        self.list = QListWidget()
        box.addWidget(self.list, 1)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit()
        name_row.addWidget(self.name_edit, 1)
        self.rename_button = QPushButton("Rename")
        name_row.addWidget(self.rename_button)
        box.addLayout(name_row)
        start_row = QHBoxLayout()
        start_row.addWidget(QLabel("Starts:"))
        self.start_combo = NoWheelComboBox()
        self._starts = budgets.period_sequence(
            budgets.shift_period(f"{today.year:04d}-{today.month:02d}", -24), 60)
        self.start_combo.addItems([_month_label(p) for p in self._starts])
        start_row.addWidget(self.start_combo, 1)
        self.move_button = QPushButton("Move start")
        self.move_button.setToolTip(
            "Move the plan's twelve months to begin here; every amount keeps "
            "its calendar month.")
        start_row.addWidget(self.move_button)
        box.addLayout(start_row)
        buttons = QHBoxLayout()
        self.new_button = QPushButton("New empty budget")
        self.copy_button = QPushButton("Copy for next year")
        self.active_button = QPushButton("Make active")
        self.delete_button = QPushButton("Delete")
        for b in (self.new_button, self.copy_button, self.active_button,
                  self.delete_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        box.addLayout(buttons)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        box.addWidget(close)
        self.list.currentRowChanged.connect(self._selected)
        self.rename_button.clicked.connect(self._rename)
        self.move_button.clicked.connect(self._move)
        self.new_button.clicked.connect(self._new)
        self.copy_button.clicked.connect(self._copy)
        self.active_button.clicked.connect(self._activate)
        self.delete_button.clicked.connect(self._delete)
        self.refresh()

    @property
    def selected_id(self) -> Optional[int]:
        i = self.list.currentRow()
        return self._ids[i] if 0 <= i < len(self._ids) else None

    def refresh(self, select: Optional[int] = None) -> None:
        keep = select if select is not None else self.selected_id
        rows = budgets.list_budgets(self.conn)
        self.list.blockSignals(True)
        try:
            self.list.clear()
            self._ids = [b.id for b in rows]
            for b in rows:
                span = (f"{_short_month(b.start_period)} to {_short_month(b.end_period)}"
                        if b.start_period and b.end_period else "")
                self.list.addItem(f"{b.name}{'  (active)' if b.active else ''}   {span}")
        finally:
            self.list.blockSignals(False)
        if self._ids:
            target = keep if keep in self._ids else next(
                (b.id for b in rows if b.active), self._ids[0])
            self.list.setCurrentRow(self._ids.index(target))
        self._selected()

    def _selected(self, *_args) -> None:
        bid = self.selected_id
        b = budgets.get_budget(self.conn, bid) if bid is not None else None
        self.name_edit.setText(b.name if b else "")
        if b and b.start_period in self._starts:
            self.start_combo.setCurrentIndex(self._starts.index(b.start_period))
        for w in (self.rename_button, self.move_button, self.copy_button,
                  self.active_button, self.delete_button):
            w.setEnabled(b is not None)

    def _rename(self) -> None:
        bid = self.selected_id
        name = self.name_edit.text().strip()
        if bid is None or not name:
            return
        budgets.rename_budget(self.conn, bid, name)
        self.refresh(bid)
        self.status.setText(f"Renamed to '{name}'.")

    def _move(self) -> None:
        bid = self.selected_id
        i = self.start_combo.currentIndex()
        if bid is None or not (0 <= i < len(self._starts)):
            return
        budgets.move_budget_start(self.conn, bid, self._starts[i])
        self.refresh(bid)
        self.status.setText(f"The plan now starts {_month_label(self._starts[i])}; "
                            f"every amount kept its calendar month.")

    def _new(self) -> None:
        start = f"{self._today.year:04d}-{self._today.month:02d}"
        bid = budgets.new_budget(self.conn, start)
        self.refresh(bid)
        self.status.setText(f"Created '{budgets.get_budget(self.conn, bid).name}', "
                            f"empty, starting {_month_label(start)}.")

    def _copy(self) -> None:
        src = self.selected_id
        if src is None:
            return
        source = budgets.get_budget(self.conn, src)
        start = budgets.shift_period(source.start_period, 12) if source.start_period \
            else f"{self._today.year + 1:04d}-{self._today.month:02d}"
        bid = budgets.new_budget(self.conn, start, copy_from=src)
        self.refresh(bid)
        self.status.setText(f"Copied '{source.name}' to a plan starting "
                            f"{_month_label(start)}. Rename it, and make it active "
                            f"when the year turns.")

    def _activate(self) -> None:
        bid = self.selected_id
        if bid is None:
            return
        budgets.set_only_active(self.conn, bid)
        self.refresh(bid)
        self.status.setText(f"'{budgets.get_budget(self.conn, bid).name}' is now the "
                            f"budget the page shows.")

    def _delete(self) -> None:
        bid = self.selected_id
        b = budgets.get_budget(self.conn, bid) if bid is not None else None
        if b is None:
            return
        answer = QMessageBox.question(
            self, "Delete Budget",
            f"Delete the budget '{b.name}' and every amount in it? Your "
            f"transactions and categories are not affected.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        budgets.delete_budget(self.conn, bid)
        remaining = budgets.list_budgets(self.conn)
        if remaining and not any(x.active for x in remaining):
            budgets.set_only_active(self.conn, remaining[0].id)
        self.refresh()
        self.status.setText(f"Deleted '{b.name}'.")


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
class BudgetPage(QWidget):
    """View > Budget. One month of the active budget as one list, with Add a
    line and More; obeys the ``mark_stale`` / ``refresh_if_stale`` contract of
    the other pages in the central stack, because every figure on it is derived
    from transactions."""

    #: Re-emitted from More > Send spending to the Retirement Planner: a basis
    #: the user accepted, for the window that owns both pages to carry across
    #: (SRD 5.12i). The page knows nothing about the other side.
    basis_ready = pyqtSignal(object)

    LINE, KIND, PLANNED, SPENT, LEFT = range(5)
    HEADERS = ("", "Kind", "Planned", "Spent", "Remaining")

    def __init__(self, conn, parent=None, *, today: Optional[_dt.date] = None):
        super().__init__(parent)
        self.conn = conn
        self._today = today
        self._stale = True
        self._filling = False
        #: row -> PageLine, or None for a section header row.
        self._rows: list[Optional[PageLine]] = []
        self._lines: list[PageLine] = []
        self._paths: dict[int, str] = {}
        self._periods: list[str] = []
        self._drill: Optional[QDialog] = None
        self._build()

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        title = QLabel("Budget for")
        font = title.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        title.setFont(font)
        bar.addWidget(title)
        self.prev_button = QToolButton()
        self.prev_button.setText("<")
        bar.addWidget(self.prev_button)
        self.month_combo = NoWheelComboBox()
        self.month_combo.setMinimumWidth(170)
        bar.addWidget(self.month_combo)
        self.next_button = QToolButton()
        self.next_button.setText(">")
        bar.addWidget(self.next_button)
        self.budget_label = QLabel("")
        bar.addWidget(self.budget_label)
        bar.addStretch(1)
        self.start_button = QPushButton("Start a budget")
        self.start_button.setToolTip("Begin an empty twelve-month plan from this month.")
        bar.addWidget(self.start_button)
        self.add_button = QPushButton("Add a line")
        bar.addWidget(self.add_button)
        # The gear is the application's customization affordance everywhere
        # else (report windows, the dashboard); here it opens the page's menu.
        self.gear_button = QToolButton()
        self.gear_button.setText("\u2699")
        self.gear_button.setToolTip("Budget options")
        self.gear_button.setAutoRaise(True)
        self.gear_button.setStyleSheet("QToolButton { font-size: 20px; padding: 2px 6px; }")
        self.gear_button.setPopupMode(QToolButton.InstantPopup)
        self.gear_menu = QMenu(self.gear_button)
        self.tracking_action = self.gear_menu.addAction("Show spending")
        self.tracking_action.setCheckable(True)
        self.tracking_action.setChecked(prefs.budget_show_tracking())
        self.tracking_action.setToolTip(
            "Show the Spent and Remaining columns and the month's reading. Off, the "
            "page is the plan alone.")
        self.scheduled_action = self.gear_menu.addAction("Count scheduled bills as spent")
        self.scheduled_action.setCheckable(True)
        self.scheduled_action.setChecked(prefs.budget_count_scheduled())
        self.gear_menu.addSeparator()
        self.scope_action = self.gear_menu.addAction("Accounts and categories...")
        self.sort_action = self.gear_menu.addAction("Sort lines by amount")
        self.year_action = self.gear_menu.addAction("Plan the year...")
        self.propose_action = self.gear_menu.addAction("Propose a plan from last year")
        self.gear_menu.addSeparator()
        self.budgets_action = self.gear_menu.addAction("Budgets...")
        self.retire_action = self.gear_menu.addAction(
            "Send spending to the Retirement Planner...")
        self.gear_button.setMenu(self.gear_menu)
        bar.addWidget(self.gear_button)
        outer.addLayout(bar)

        # The two sentences sit ABOVE the table, where the eye lands first: the
        # balance is the point of the page, not a footnote under it.
        self.plan_label = QLabel("")
        self.plan_label.setWordWrap(True)
        pf = self.plan_label.font()
        pf.setBold(True)
        self.plan_label.setFont(pf)
        outer.addWidget(self.plan_label)
        self.month_label = QLabel("")
        self.month_label.setWordWrap(True)
        outer.addWidget(self.month_label)
        self.legend_label = QLabel(
            "F = Fixed, a cost you carry (rent, insurance, a payment): reported, "
            "not graded.   V = Variable, spending you can change: graded each "
            "month. Click a letter to switch.   " + PLANNED_HINT)
        self.legend_label.setWordWrap(True)
        self.legend_label.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.legend_label)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        # Typing a Planned amount changes the shown month only, which is not
        # obvious from the cell; say so where the eye is.
        self.table.horizontalHeaderItem(self.PLANNED).setToolTip(PLANNED_HINT)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        head = self.table.horizontalHeader()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(self.LINE, QHeaderView.Stretch)
        for col in range(self.KIND, len(self.HEADERS)):
            head.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        outer.addWidget(self.table, 1)

        # Outcomes of an action ("Removed Rent") go to the window's status bar
        # for a few seconds; nothing accumulates on the page. The first build
        # kept a status label under the table and it read as a log.
        self.status = QLabel("")
        self.status.setVisible(False)

        self.prev_button.clicked.connect(lambda: self._step(-1))
        self.next_button.clicked.connect(lambda: self._step(1))
        self.month_combo.currentIndexChanged.connect(self._month_chosen)
        self.start_button.clicked.connect(self._start_budget)
        self.add_button.clicked.connect(self._add_line)
        self.year_action.triggered.connect(self._plan_year)
        self.propose_action.triggered.connect(self._propose_plan)
        self.tracking_action.toggled.connect(self._tracking_toggled)
        self.scope_action.triggered.connect(self._scope_dialog)
        self.sort_action.triggered.connect(self._sort_by_amount)
        self.scheduled_action.toggled.connect(self._count_scheduled_toggled)
        self.budgets_action.triggered.connect(self._budgets_dialog)
        self.retire_action.triggered.connect(self._send_to_retirement)
        self.table.cellChanged.connect(self._cell_changed)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.customContextMenuRequested.connect(self._context_menu)

    # -- the stale-page contract --------------------------------------------
    def mark_stale(self) -> None:
        self._stale = True
        if self.isVisible():
            self.refresh_if_stale()

    def refresh_if_stale(self) -> bool:
        if not self._stale:
            return False
        self.refresh()
        return True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_if_stale()

    # -- what is selected ---------------------------------------------------
    @property
    def today(self) -> _dt.date:
        return self._today or _dt.date.today()

    def _current_period(self) -> str:
        return f"{self.today.year:04d}-{self.today.month:02d}"

    @property
    def budget(self) -> Optional[budgets.Budget]:
        active = budgets.list_budgets(self.conn, include_inactive=False)
        return active[0] if active else None

    @property
    def budget_id(self) -> Optional[int]:
        b = self.budget
        return b.id if b is not None else None

    @property
    def period(self) -> str:
        i = self.month_combo.currentIndex()
        return self._periods[i] if 0 <= i < len(self._periods) else ""

    @property
    def count_scheduled(self) -> bool:
        return self.scheduled_action.isChecked()

    @property
    def tracking(self) -> bool:
        """Whether Spent, Remaining and the month's reading are shown."""
        return self.tracking_action.isChecked()

    def say(self, text: str) -> None:
        """Report an action's outcome: the window's status bar for a few
        seconds when there is one, and ``status`` (unseen) for tests."""
        self.status.setText(text)
        top = self.window()
        bar = getattr(top, "statusBar", None)
        if bar is not None and top is not self:
            try:
                bar().showMessage(text, 6000)
            except Exception:                 # pragma: no cover - no status bar
                pass

    def lines(self) -> list[PageLine]:
        """The lines the page is showing, in display order. Public for tests."""
        return list(self._lines)

    # -- reading ------------------------------------------------------------
    def refresh(self) -> None:
        self._stale = False
        self._paths = {c["id"]: c["path"]
                       for c in ledger.list_categories(self.conn, include_hidden=True)}
        self._fill_months()
        self._fill_table()

    def _fill_months(self) -> None:
        keep = self.period
        b = self.budget
        self._filling = True
        try:
            self.month_combo.clear()
            if b is None or not b.start_period:
                self._periods = []
                return
            self._periods = budgets.plan_periods(b.start_period)
            self.month_combo.addItems([_month_label(p) for p in self._periods])
            target = keep if keep in self._periods else self._current_period()
            if target not in self._periods:
                target = self._periods[0]
            self.month_combo.setCurrentIndex(self._periods.index(target))
        finally:
            self._filling = False

    def _fill_table(self) -> None:
        b, period = self.budget, self.period
        tbl = self.table
        self._filling = True
        try:
            tbl.setRowCount(0)
            self._rows = []
            self._lines = []
            has = b is not None and bool(period)
            self.start_button.setVisible(b is None)
            self.add_button.setEnabled(has)
            for act in (self.year_action, self.propose_action, self.budgets_action,
                        self.retire_action, self.scope_action, self.sort_action):
                act.setEnabled(b is not None)
            tbl.setColumnHidden(self.SPENT, not self.tracking)
            tbl.setColumnHidden(self.LEFT, not self.tracking)
            self.month_label.setVisible(self.tracking)
            self.prev_button.setEnabled(has and self.month_combo.currentIndex() > 0)
            self.next_button.setEnabled(
                has and self.month_combo.currentIndex() < len(self._periods) - 1)
            self.budget_label.setText(f"({b.name})" if b is not None else "")
            if not has:
                self.plan_label.setText(
                    "No budget yet. Start a budget to plan this month's take-home "
                    "pay and spending as one list.")
                self.month_label.setText("")
                return
            self._lines = page_lines(self.conn, b.id, period, paths=self._paths)
            income = [ln for ln in self._lines if ln.kind == "income"]
            expenses = [ln for ln in self._lines if ln.is_expense]
            tbl.setRowCount(len(self._lines) + 3 + (0 if income else 1))
            r = 0
            r = self._put_header(r, "Income")
            for ln in income:
                r = self._put_line(r, ln)
            if not income:
                # The lesson's first line, and the one a new budgeter could not
                # find: say where it comes from, on the row where it will be.
                r = self._put_hint(r, "Add your take-home pay: click here, or "
                                      "Add a line and choose Income.")
            r = self._put_header(r, "Expenses")
            for ln in expenses:
                r = self._put_line(r, ln)
            r = self._put_totals(r, expenses)
            self.plan_label.setText(plan_sentence(self._lines))
            self.month_label.setText(month_sentence(
                self._lines, period, self.today,
                count_scheduled=self.count_scheduled))
        finally:
            self._filling = False

    #: Marks the totals row.
    TOTALS = "totals"

    def _put_totals(self, r: int, expenses: list) -> int:
        """The expense total: planned, spent and left over every expense line,
        Everything else included. Bold, last, never a line of its own."""
        self._rows.append(self.TOTALS)
        planned = sum(ln.planned_cents for ln in expenses)
        spent = sum(ln.charged(count_scheduled=self.count_scheduled) for ln in expenses)
        label = QTableWidgetItem("    Total")
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        label.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(r, self.LINE, label)
        blank = QTableWidgetItem("")
        blank.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(r, self.KIND, blank)
        for col, cents in ((self.PLANNED, planned), (self.SPENT, spent)):
            cell = _CentsItem(cents)
            cell.setFont(font)
            self.table.setItem(r, col, cell)
        left = QTableWidgetItem(fmt_cents(planned - spent))   # negative = over
        _mark_negative(left, planned - spent)
        left.setFont(font)
        left.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        left.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(r, self.LEFT, left)
        return r + 1

    #: Marks the clickable hint row in an empty Income section.
    INCOME_HINT = "income-hint"

    def _put_hint(self, r: int, text: str) -> int:
        self._rows.append(self.INCOME_HINT)
        item = QTableWidgetItem("    " + text)
        font = item.font()
        font.setItalic(True)
        item.setFont(font)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        item.setToolTip("Opens Add a line with Income chosen.")
        self.table.setItem(r, self.LINE, item)
        for col in range(self.KIND, len(self.HEADERS)):
            blank = QTableWidgetItem("")
            blank.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(r, col, blank)
        return r + 1

    def _put_header(self, r: int, text: str) -> int:
        self._rows.append(None)
        item = QTableWidgetItem(text)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(r, self.LINE, item)
        for col in range(self.KIND, len(self.HEADERS)):
            blank = QTableWidgetItem("")
            blank.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, col, blank)
        return r + 1

    def _put_line(self, r: int, ln: PageLine) -> int:
        self._rows.append(ln)
        label = "    " + ln.label + (f"   {ln.detail}" if ln.detail else "")
        name = QTableWidgetItem(label)
        name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if ln.kind == "group":
            name.setToolTip("One line covering several categories; its Spent is "
                            "their spending together.")
        elif ln.kind == "other":
            name.setToolTip("Spending in categories that have no line of their own. "
                            "Double-click Spent to see which. Give it a planned "
                            "amount and it takes part in the balance.")
        self.table.setItem(r, self.LINE, name)
        kind = QTableWidgetItem(KIND_LETTER.get(ln.bucket, "") if ln.can_toggle_kind else "")
        kind.setTextAlignment(Qt.AlignCenter)
        kind.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if ln.can_toggle_kind:
            kind.setToolTip(KIND_TOOLTIP)
        self.table.setItem(r, self.KIND, kind)
        planned = QTableWidgetItem(fmt_cents(ln.planned_cents) if ln.planned_cents
                                   or ln.kind != "other" else "")
        planned.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        planned.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable)
        _mark_negative(planned, ln.planned_cents)
        planned.setToolTip(PLANNED_HINT)
        self.table.setItem(r, self.PLANNED, planned)
        spent = _CentsItem(ln.charged(count_scheduled=self.count_scheduled))
        tip = self.spent_tooltip(ln)
        if ln.committed_cents and self.count_scheduled:
            tip = (f"{fmt_cents(ln.spent_cents)} entered plus "
                   f"{fmt_cents(ln.committed_cents)} scheduled and not yet entered."
                   + ("\n" + tip if tip else ""))
        if tip:
            spent.setToolTip(tip)
        self.table.setItem(r, self.SPENT, spent)
        left = QTableWidgetItem(left_text(ln, count_scheduled=self.count_scheduled))
        _mark_negative(left, left_cents(ln, count_scheduled=self.count_scheduled))
        left.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        left.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        carried = left_tooltip(ln)
        if carried:
            left.setToolTip(carried)
        self.table.setItem(r, self.LEFT, left)
        return r + 1

    # -- month navigation ---------------------------------------------------
    def _step(self, delta: int) -> None:
        i = self.month_combo.currentIndex() + delta
        if 0 <= i < len(self._periods):
            self.month_combo.setCurrentIndex(i)

    def _month_chosen(self, _index: int) -> None:
        if not self._filling:
            self._fill_table()

    # -- editing ------------------------------------------------------------
    def _line_at(self, row: int) -> Optional[PageLine]:
        entry = self._rows[row] if 0 <= row < len(self._rows) else None
        return entry if isinstance(entry, PageLine) else None

    def _is_hint(self, row: int) -> bool:
        return 0 <= row < len(self._rows) and self._rows[row] == self.INCOME_HINT

    def _cell_changed(self, row: int, col: int) -> None:
        if self._filling or col != self.PLANNED:
            return
        ln = self._line_at(row)
        bid = self.budget_id
        if ln is None or bid is None:
            return
        item = self.table.item(row, col)
        text = (item.text() if item is not None else "").strip()
        self._freeze_order()
        cents = parse_amount(text) if text else None
        write_line(self.conn, bid, ln, self.period, cents)
        if ln.kind == "account":
            goal = goals.goal_for_account(self.conn, ln.ident, budget_id=bid)
            if goal is not None:
                goals.update_goal(self.conn, goal.id, monthly_cents=cents or 0)
        self._fill_table()

    def _freeze_order(self) -> None:
        """Pin the lines where they are, once, the first time the plan is
        edited: the default order is by amount, and a row that moved every time
        its amount was typed would be the annoyance the user named. Stored as
        the user's own order; Sort lines by amount under the gear clears it."""
        bid = self.budget_id
        if bid is None or budgets.line_order(self.conn, bid):
            return
        ordered = [ln.order_key for ln in self._lines if ln.kind != "other"]
        if ordered:
            budgets.set_line_order(self.conn, bid, ordered)

    def _sort_by_amount(self) -> None:
        bid = self.budget_id
        if bid is None:
            return
        budgets.set_line_order(self.conn, bid, [])
        self._fill_table()
        self.say("Lines are in order of planned amount, largest first.")

    def _cell_clicked(self, row: int, col: int) -> None:
        if self._is_hint(row):
            self._add_line(purpose=FOR_INCOME)
            return
        if col != self.KIND:
            return
        ln = self._line_at(row)
        if ln is None or not ln.can_toggle_kind:
            return
        self._toggle_kind(ln)

    def _toggle_kind(self, ln: PageLine) -> None:
        bid = self.budget_id
        new = "flex" if ln.bucket == "fixed" else "fixed"
        if ln.kind == "group":
            budgets.update_group(self.conn, ln.ident, bucket=new)
        else:
            budgets.set_settings(self.conn, bid, ln.ident, bucket=new)
        self._fill_table()
        self.say(f"{ln.label} is now {KIND_WORD[new]}.")

    def _cell_double_clicked(self, row: int, col: int) -> None:
        if col == self.SPENT:
            self.drill_down(row)

    def drill_down(self, row: int) -> Optional[QDialog]:
        """The transactions behind one line's Spent, shown and never
        exec_()-ed; None for a header row or an income or saving line."""
        ln = self._line_at(row)
        period = self.period
        if ln is None or not period or ln.kind in ("income", "account"):
            return None
        start, end = budgets.period_bounds(period)
        dlg = LineDrillDownDialog(
            self.conn, title=f"{ln.label} - {_month_label(period)}",
            start=start, end=end, category_ids=ln.members,
            account_ids=budgets.spending_account_ids(self.conn),
            include_uncategorized=ln.kind == "other",
            payee_match=ln.payee_match, parent=self)
        self._drill = dlg
        dlg.show()
        return dlg

    def spent_tooltip(self, ln: PageLine) -> str:
        """The transactions behind a line's Spent, a few lines of them, for the
        hover: date, payee and amount, newest first, and how many more there
        are. The same rows the drill-down lists, through the same report."""
        period = self.period
        if not period or ln.kind == "income" and not ln.spent_cents:
            return ""
        start, end = budgets.period_bounds(period)
        prefix = ""
        if ln.kind == "account":
            prefix = (self._payoff_words(ln) if ln.account_type == "liability"
                      else self._goal_words(ln))
        rows = spent_rows(self.conn, ln, start, end)
        if not rows:
            return (prefix + "\n" if prefix else "") + "Nothing yet this month."
        shown = rows[:8]
        lines = [f"{fmt_date(d)}  {payee or '(no payee)'}  {fmt_cents(cents)}"
                 for d, payee, cents in shown]
        if len(rows) > len(shown):
            lines.append(f"and {len(rows) - len(shown)} more")
        lines.append("Double-click for the full list.")
        if prefix:
            lines.insert(0, prefix)
        return "\n".join(lines)

    # -- the row menu -------------------------------------------------------
    def _context_menu(self, pos: QPoint) -> None:
        row = self.table.rowAt(pos.y())
        menu = self.menu_for_row(row)
        if menu is not None:
            menu.exec_(self.table.viewport().mapToGlobal(pos))

    def menu_for_row(self, row: int) -> Optional[QMenu]:
        """The right-click menu for any row of the table: a line's own menu, or
        for a section header and the income hint a way to add a line of that
        section's kind - so the Income section can be filled from where it sits
        even when it is empty. None for a row that is nothing. Public for tests."""
        ln = self._line_at(row)
        if ln is not None:
            return self.row_menu(ln)
        if self._is_hint(row) or self._header_text(row) == "Income":
            menu = QMenu(self)
            act = menu.addAction("Add income...")
            act.triggered.connect(lambda: self._add_line(purpose=FOR_INCOME))
            return menu
        if self._header_text(row) == "Expenses":
            menu = QMenu(self)
            act = menu.addAction("Add a line...")
            act.triggered.connect(lambda: self._add_line(purpose=FOR_SPENDING))
            return menu
        return None

    def _header_text(self, row: int) -> str:
        if not (0 <= row < len(self._rows)) or self._rows[row] is not None:
            return ""
        item = self.table.item(row, self.LINE)
        return item.text() if item is not None else ""

    def row_menu(self, ln: PageLine) -> QMenu:
        """The actions for one line, built but not shown. Edit comes first:
        it is what a right-click on a line is for. Public for tests."""
        menu = QMenu(self)
        if ln.kind == "other":
            act = menu.addAction("Add a line...")
            act.triggered.connect(lambda: self._add_line(purpose=FOR_SPENDING))
            return menu
        edit = menu.addAction("Edit income..." if ln.kind == "income"
                              else "Edit line...")
        edit.triggered.connect(lambda: self.change_line(ln))
        menu.addSeparator()
        up = menu.addAction("Move up")
        up.triggered.connect(lambda: self.move_line(ln, -1))
        down = menu.addAction("Move down")
        down.triggered.connect(lambda: self.move_line(ln, 1))
        if ln.kind in ("category", "group"):
            carry = menu.addAction("Carry unspent amounts forward")
            carry.setCheckable(True)
            carry.setChecked(ln.rollover_mode != "none")
            carry.setToolTip("What this line does not spend in a month is added to "
                             "the next month's amount; an overspend is forgiven.")
            carry.triggered.connect(lambda on: self.set_carry(ln, on))
        menu.addSeparator()
        remove = menu.addAction("Remove line")
        remove.triggered.connect(lambda: self.remove_line(ln))
        return menu

    def move_line(self, ln: PageLine, delta: int) -> None:
        """Move a line one place within its section and remember the order."""
        bid = self.budget_id
        if bid is None:
            return
        section = [x for x in self._lines if x.kind != "other"
                   and (x.kind == "income") == (ln.kind == "income")]
        keys = [x.order_key for x in section]
        if ln.order_key not in keys:
            return
        i = keys.index(ln.order_key)
        j = i + delta
        if not (0 <= j < len(keys)):
            return
        keys[i], keys[j] = keys[j], keys[i]
        others = [x.order_key for x in self._lines if x.kind != "other"
                  and (x.kind == "income") != (ln.kind == "income")]
        ordered = keys + others if ln.kind == "income" else others + keys
        budgets.set_line_order(self.conn, bid, ordered)
        self._fill_table()

    def set_carry(self, ln: PageLine, on: bool) -> None:
        bid = self.budget_id
        mode = "positive" if on else "none"
        if ln.kind == "group":
            budgets.update_group(self.conn, ln.ident, rollover_mode=mode)
        else:
            budgets.set_settings(self.conn, bid, ln.ident, rollover_mode=mode)
        self._fill_table()
        self.say(f"{ln.label} now carries unspent amounts forward." if on else
                 f"{ln.label} no longer carries anything forward.")

    def remove_line(self, ln: PageLine) -> None:
        bid = self.budget_id
        if bid is None or ln.kind == "other":
            return
        answer = QMessageBox.question(
            self, "Remove line",
            f"Remove '{ln.label}' and its planned amounts from this budget? Your "
            f"transactions are not affected.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if ln.kind == "group":
            budgets.delete_group(self.conn, ln.ident)
        elif ln.kind == "account":
            budgets.remove_saving_account(self.conn, bid, ln.ident)
            goal = goals.goal_for_account(self.conn, ln.ident, budget_id=bid)
            if goal is not None:
                goals.delete_goal(self.conn, goal.id)     # the goal is the line's
        else:
            budgets.remove_category(self.conn, bid, ln.ident)
        self._fill_table()
        self.say(f"Removed {ln.label}.")

    # -- adding and changing lines ------------------------------------------
    def _run_dialog(self, dialog: QDialog) -> bool:
        """Show ``dialog`` modally; True when accepted. The one ``exec_()`` on
        the page, behind a seam tests override (offscreen, it never returns)."""
        return dialog.exec_() == QDialog.Accepted

    def make_add_dialog(self, *, exclude_current: bool = True,
                        only: Optional[PageLine] = None) -> AddLineDialog:
        """The Add a line dialog, built but not shown. Public so a test can fill
        one in. With ``only``, the choices are narrowed to that line's own
        categories or account (the row menu's "change" path)."""
        from mammon import category_types
        from mammon.reports.saving import CASH_FLOW_ACCOUNT_TYPES

        bid = self.budget_id
        scope = budgets.budget_account_ids(self.conn, bid) if bid is not None else None
        history = budgets.trailing_samples(self.conn, today=self.today,
                                           account_ids=scope)
        types = category_types.classify_categories(self.conn)
        on_plan: set[int] = set(budgets.excluded_categories(self.conn, bid)
                                if bid is not None else ())
        on_accounts: set[int] = set()
        if exclude_current and bid is not None and only is None:
            for ln in self._lines:
                if ln.kind in ("category", "income"):
                    on_plan.add(ln.ident)
                elif ln.kind == "group":
                    on_plan.update(ln.members)
                elif ln.kind == "account":
                    on_accounts.add(ln.ident)
        keep_cats = set(only.members) if only is not None and only.kind != "account" else None
        keep_acct = only.ident if only is not None and only.kind == "account" else None

        def kind_of(cid: int) -> str:
            # The ledger's own net decides what is income (category_types).
            return types.get(cid, "expense")

        def cats(kind: str, samples: dict, *, any_kind: bool = False) -> list[LineChoice]:
            out = []
            for c in ledger.list_categories(self.conn):
                cid = c["id"]
                if c["path"].split(":")[-1] == "--Split--" or cid in on_plan:
                    continue
                if keep_cats is not None and cid not in keep_cats:
                    continue
                if not any_kind and kind_of(cid) != kind:
                    continue
                out.append(LineChoice(id=cid, label=c["path"],
                                      samples=tuple(samples.get(cid, ()))))
            out.sort(key=lambda c: (-c.total, c.label.lower()))
            return out

        income_choices = cats("income", history.income)
        income_note = ""
        if not income_choices and keep_cats is None:
            # A brand-new ledger: nothing has been received yet, so no category
            # reads as income. Offer them all rather than an empty list, and say
            # why - the alternative is a page with no way to set the income.
            income_choices = cats("income", history.income, any_kind=True)
            income_note = ("No category has received income yet. Pick the one "
                           "your pay will be entered under (Salary, Net pay), or "
                           "make one in Tools > Category Manager.")

        accounts, debts = [], []
        today_iso = self.today.isoformat()
        # Which debt accounts are worth paying down is debt.py's call, not this
        # page's: an account with nothing owed is not a payoff candidate. The
        # line being changed stays offered even at zero, so the row menu's
        # "change" path on a just-paid-off line is not an empty list.
        owing = {int(a["id"]) for a in
                 debt.debt_accounts(self.conn, as_of=today_iso)}
        for a in ledger.list_accounts(self.conn):
            aid = int(a["id"])
            acct_kind = a["type"] or ""
            if (aid in on_accounts or acct_kind in CASH_FLOW_ACCOUNT_TYPES
                    or (keep_acct is not None and aid != keep_acct)):
                continue
            balance = ledger.account_balance(self.conn, aid, today_iso)
            if acct_kind == "liability":
                if aid not in owing and keep_acct != aid:
                    continue
                terms = debt.get_terms(self.conn, aid)
                owed = max(0, -int(balance))
                synthesized = None if terms is not None else debt.project_debt(
                    self.conn, aid, 1, as_of=today_iso) if owed else None
                apr = (str(terms.apr) if terms is not None else
                       str(synthesized.debts[0].apr) if synthesized and synthesized.debts
                       else None)
                # History is the EXTRA principal paid each month: the regular
                # payment's principal belongs to the line that pays it.
                extra = tuple(
                    budgets.month_extra_principal(self.conn, p, [aid]).get(aid, 0)
                    for p in history.periods)
                choice = LineChoice(id=aid, label=a["name"], samples=extra,
                                    balance_cents=owed, apr=apr,
                                    estimated=terms is None)
                debts.append(choice)
            else:
                accounts.append(LineChoice(
                    id=aid, label=a["name"],
                    samples=tuple(history.saving.get(aid, ())),
                    balance_cents=int(balance)))
        for lst in (accounts, debts):
            lst.sort(key=lambda c: (-c.total, c.label.lower()))
        periods = list(history.periods)
        dlg = AddLineDialog(
            history=history, categories=cats("expense", history.spending),
            income_categories=income_choices,
            accounts=accounts, debts=debts,
            bill_schedules=budgets.bill_schedule_summary(self.conn),
            income_schedules=budgets.income_schedule_summary(self.conn),
            periods=list(self._periods), income_note=income_note,
            payee_samples=lambda match: budgets.payee_history(self.conn, match, periods),
            project_debt=lambda aid, cents, apr: debt.project_extra(
                self.conn, aid, cents, as_of=today_iso,
                apr=Decimal(apr) if apr else None),
            today=self.today, parent=self)
        if only is None and not any(ln.kind == "income" for ln in self._lines):
            # The lesson's first line is income; until the plan has one, that is
            # what Add a line is for.
            dlg.purpose_combo.setCurrentText(FOR_INCOME)
        return dlg

    def _add_line(self, *, purpose: Optional[str] = None) -> None:
        bid = self.budget_id
        if bid is None:
            return
        dialog = self.make_add_dialog()
        if purpose is not None:
            dialog.purpose_combo.setCurrentText(purpose)
        try:
            if not self._run_dialog(dialog):
                return
            req = dialog.result()
        finally:
            dialog.deleteLater()
        if req is None:
            return
        self.apply_request(req)

    def change_line(self, ln: PageLine) -> None:
        """Re-plan one line: the same dialog narrowed to the line, and its
        months rewritten from the answer."""
        bid = self.budget_id
        if bid is None:
            return
        dialog = self.make_add_dialog(only=ln)
        purpose = (FOR_INCOME if ln.kind == "income" else
                   FOR_DEBT if ln.account_type == "liability" else
                   FOR_SAVING if ln.kind == "account" else
                   FOR_PAYEE if ln.by_payee else FOR_SPENDING)
        dialog.purpose_combo.setCurrentText(purpose)
        if ln.by_payee:
            dialog.payee_edit.setText(ln.payee_match or "")
            dialog.name_edit.setProperty("auto", False)
        else:
            dialog.choose(ln.members if ln.kind != "account" else (ln.ident,))
        if ln.kind == "group":
            dialog.name_edit.setText(ln.label)
        if ln.kind in ("category", "group"):
            (dialog.fixed_radio if ln.bucket == "fixed" else dialog.variable_radio
             ).setChecked(True)
        if ln.kind == "account" and ln.account_type != "liability":
            goal = goals.goal_for_account(self.conn, ln.ident, budget_id=bid)
            if goal is not None:
                dialog.goal_edit.setText(fmt_cents(goal.target_cents))
                if goal.target_date:
                    from PyQt5.QtCore import QDate
                    dialog.goal_date_edit.setDate(
                        QDate.fromString(goal.target_date, "yyyy-MM-dd"))
        try:
            if not self._run_dialog(dialog):
                return
            req = dialog.result()
        finally:
            dialog.deleteLater()
        if req is None:
            return
        for period in self._periods:
            write_line(self.conn, bid, ln, period, None)
        self.apply_request(req, existing=ln)

    def apply_request(self, req: AddLineRequest, *,
                      existing: Optional[PageLine] = None) -> None:
        """Write a collected request through the domain layer: the lines, the
        kind, and for several categories the group that covers them."""
        bid = self.budget_id
        b = budgets.get_budget(self.conn, bid)
        start = b.start_period
        target: dict = {}
        label = req.name
        if req.purpose == FOR_PAYEE:
            if existing is not None and existing.kind == "group":
                gid = existing.ident
                budgets.update_group(self.conn, gid, name=req.name, bucket=req.bucket,
                                     payee_match=req.payee_match)
            else:
                gid = budgets.create_group(self.conn, bid, req.name, bucket=req.bucket,
                                           payee_match=req.payee_match)
            target = {"group_id": gid}
        elif req.purpose == FOR_SPENDING and len(req.ids) > 1:
            if existing is not None and existing.kind == "group":
                gid = existing.ident
                budgets.update_group(self.conn, gid, name=req.name, bucket=req.bucket,
                                     payee_match=None)
            else:
                gid = budgets.create_group(self.conn, bid, req.name, bucket=req.bucket)
            budgets.set_group_members(self.conn, gid, req.ids)
            target = {"group_id": gid}
        elif req.purpose in (FOR_SPENDING, FOR_INCOME):
            cid = req.ids[0]
            target = {"category_id": cid}
            label = self._paths.get(cid, req.name)
            if req.purpose == FOR_INCOME:
                budgets.set_settings(self.conn, bid, cid, bucket="income")
            elif req.how != YEARLY_BILL:
                budgets.set_settings(self.conn, bid, cid, bucket=req.bucket)
        else:
            target = {"account_id": req.ids[0]}
            self._apply_account_extras(req, existing)
        written: list[str] = []
        if req.how == YEARLY_BILL and "category_id" in target:
            budgets.apply_nonmonthly(self.conn, bid, target["category_id"],
                                     req.amount_cents, start_period=start)
            written = self._periods
        elif req.how == YEARLY_BILL and "group_id" in target:
            budgets.apply_nonmonthly_group(self.conn, target["group_id"],
                                           req.amount_cents, start_period=start)
            written = self._periods
        elif req.how == CERTAIN_MONTHS:
            kind = ("income" if req.purpose == FOR_INCOME else
                    "group" if "group_id" in target else
                    "account" if "account_id" in target else "category")
            ident = next(iter(target.values()))
            ln = PageLine(kind=kind, ident=ident, label=label)
            for period in req.months:
                write_line(self.conn, bid, ln, period, req.amount_cents)
            written = list(req.months)
        else:
            freq = {EVERY_MONTH: "monthly", EVERY_TWO_WEEKS: "biweekly",
                    EVERY_WEEK: "weekly"}[req.how]
            written = budgets.fill_line(
                self.conn, bid, req.amount_cents, frequency=freq,
                first_period=start if freq == "monthly" else None,
                first_date=req.first_date, **target)
        self._fill_table()
        self.say(f"Added {label}: {fmt_cents(req.amount_cents)} {req.how}"
                 f"{' ' + fmt_date(req.first_date) if req.first_date else ''}, in "
                 f"{len(written)} month{'s' if len(written) != 1 else ''}.")

    def _apply_account_extras(self, req: AddLineRequest,
                              existing: Optional[PageLine]) -> None:
        """A save-into line's goal and a pay-down line's rate, written through
        their own domain modules: the goal is a property of the line (created
        or updated on the account), the APR a term on the debt account."""
        bid = self.budget_id
        aid = req.ids[0]
        if req.purpose == FOR_SAVING:
            goal = goals.goal_for_account(self.conn, aid, budget_id=bid)
            if req.goal_cents:
                if goal is None:
                    today_iso = self.today.isoformat()
                    goals.create_goal(
                        self.conn, req.name, req.goal_cents, target_date=req.goal_date,
                        account_id=aid,
                        baseline_cents=ledger.account_balance(self.conn, aid, today_iso),
                        baseline_date=today_iso, budget_id=bid,
                        monthly_cents=req.amount_cents, created_at=today_iso)
                else:
                    goals.update_goal(self.conn, goal.id, target_cents=req.goal_cents,
                                      target_date=req.goal_date,
                                      monthly_cents=req.amount_cents)
            elif goal is not None and existing is not None:
                goals.delete_goal(self.conn, goal.id)      # the goal was cleared
        elif req.purpose == FOR_DEBT and req.apr is not None:
            try:
                rate = Decimal(req.apr)
            except (InvalidOperation, ValueError):
                return
            terms = debt.get_terms(self.conn, aid)
            if terms is None or terms.apr != rate:
                debt.set_apr(self.conn, aid, rate)      # keeps the payment terms

    def _goal_words(self, ln: PageLine) -> str:
        """A save-into line's goal, where it stands, for the hover."""
        goal = goals.goal_for_account(self.conn, ln.ident, budget_id=self.budget_id)
        if goal is None:
            return ""
        prog = goals.goal_progress(self.conn, goal.id, as_of=self.today.isoformat())
        head = (f"Goal {fmt_cents(prog.target_cents)}"
                + (f" by {fmt_date(prog.target_date)}" if prog.target_date else "")
                + f": {fmt_cents(prog.funded_cents)} saved, "
                f"{fmt_cents(prog.remaining_cents)} to go.")
        if prog.complete:
            return head + " Reached."
        if prog.target_date:
            head += (f" {fmt_cents(prog.required_cents)} a month needed over "
                     f"{prog.months_left} month{'s' if prog.months_left != 1 else ''}; "
                     f"planned {fmt_cents(prog.monthly_cents)}: "
                     + ("on pace." if prog.state == goals.ON_PACE
                        else f"behind by {fmt_cents(prog.shortfall_cents)} a month."))
        elif prog.projected_month:
            head += (f" At {fmt_cents(prog.monthly_cents)} a month, reached in "
                     f"{_month_label(prog.projected_month)}.")
        return head

    def _payoff_words(self, ln: PageLine) -> str:
        """A pay-down line's projection, for the hover: the regular payment
        alone, and with the line's extra principal (SRD 5.12h)."""
        try:
            proj = debt.project_extra(self.conn, ln.ident, max(0, ln.planned_cents),
                                      as_of=self.today.isoformat())
        except (KeyError, ValueError):
            return ""
        return extra_principal_sentence(proj)

    # -- More ---------------------------------------------------------------
    def _start_budget(self) -> None:
        bid = budgets.new_budget(self.conn, self._current_period())
        budgets.set_only_active(self.conn, bid)
        self.refresh()
        self.say(f"Started '{budgets.get_budget(self.conn, bid).name}' from "
                 f"{_month_label(self._current_period())}. Add a line for your "
                 f"take-home pay first, then the costs you carry, then the spending "
                 f"you can change.")

    def _plan_year(self) -> None:
        bid = self.budget_id
        if bid is None:
            return
        dialog = PlanYearDialog(self.conn, bid, lines=self._lines, parent=self)
        try:
            self._run_dialog(dialog)
        finally:
            dialog.deleteLater()
        self._fill_table()

    def _propose_plan(self) -> None:
        """One Variable line per category spent in the last twelve complete
        months, at its average, and an income line per income category at its
        average net - for categories not already on the plan. It says what it
        did; nothing is written anywhere else."""
        bid = self.budget_id
        if bid is None:
            return
        from mammon import category_types
        types = category_types.classify_categories(self.conn)
        history = budgets.trailing_samples(
            self.conn, today=self.today,
            account_ids=budgets.budget_account_ids(self.conn, bid))
        start = budgets.get_budget(self.conn, bid).start_period
        on_plan: set[int] = set(budgets.excluded_categories(self.conn, bid))
        for ln in self._lines:
            if ln.kind in ("category", "income"):
                on_plan.add(ln.ident)
            elif ln.kind == "group":
                on_plan.update(ln.members)
        added = 0
        for cid, samples in sorted(history.spending.items()):
            if cid in on_plan or types.get(cid) != "expense":
                continue
            mean = budgets.mean_cents(sum(samples), max(len(samples), 1))
            if mean <= 0:
                continue
            budgets.fill_line(self.conn, bid, mean, frequency="monthly",
                              first_period=start, category_id=cid)
            added += 1
        for cid, samples in sorted(history.income.items()):
            if cid in on_plan or types.get(cid) != "income":
                continue
            mean = budgets.mean_cents(sum(samples), max(len(samples), 1))
            if mean <= 0:
                continue
            budgets.set_settings(self.conn, bid, cid, bucket="income")
            budgets.fill_line(self.conn, bid, mean, frequency="monthly",
                              first_period=start, category_id=cid)
            added += 1
        self._fill_table()
        self.say(f"Proposed {added} line{'s' if added != 1 else ''} from the last "
                 f"{len(history.periods)} complete months, each at its monthly "
                 f"average and marked Variable. Change any amount, switch a line to "
                 f"Fixed, or remove what you do not want." if added else
                 "Nothing to propose: every category with spending is already on "
                 "the plan.")

    def _count_scheduled_toggled(self, on: bool) -> None:
        prefs.set_budget_count_scheduled(bool(on))
        self._fill_table()

    def _tracking_toggled(self, on: bool) -> None:
        prefs.set_budget_show_tracking(bool(on))
        self._fill_table()

    def make_scope_dialog(self) -> "BudgetScopeDialog":
        """The accounts-and-categories dialog, built but not shown."""
        bid = self.budget_id
        return BudgetScopeDialog(
            accounts=[(int(a["id"]), a["name"]) for a in ledger.list_accounts(self.conn)
                      if (a["type"] or "") in budgets.SPENDING_ACCOUNT_TYPES],
            chosen=budgets.budget_account_ids(self.conn, bid),
            categories=[(c["id"], c["path"]) for c in ledger.list_categories(self.conn)
                        if c["path"].split(":")[-1] != "--Split--"],
            excluded=budgets.excluded_categories(self.conn, bid), parent=self)

    def _scope_dialog(self) -> None:
        bid = self.budget_id
        if bid is None:
            return
        dialog = self.make_scope_dialog()
        try:
            if not self._run_dialog(dialog):
                return
            accounts, excluded = dialog.result()
        finally:
            dialog.deleteLater()
        every = {int(a["id"]) for a in ledger.list_accounts(self.conn)
                 if (a["type"] or "") in budgets.SPENDING_ACCOUNT_TYPES}
        budgets.set_budget_accounts(self.conn, bid,
                                    None if set(accounts) >= every else accounts)
        budgets.set_excluded_categories(self.conn, bid, excluded)
        self._fill_table()
        self.say(f"The budget covers {len(accounts)} account"
                 f"{'s' if len(accounts) != 1 else ''} and leaves out {len(excluded)} "
                 f"categor{'y' if len(excluded) == 1 else 'ies'}.")

    def _budgets_dialog(self) -> None:
        dialog = BudgetsDialog(self.conn, today=self.today, parent=self)
        try:
            self._run_dialog(dialog)
        finally:
            dialog.deleteLater()
        self.refresh()

    def make_basis_dialog(self) -> BudgetBasisDialog:
        """The subtraction table for the Retirement Planner, built but not shown
        (SRD 5.12i). No retirement year is passed: this page holds none."""
        as_of = budgets.period_bounds(self.period)[1] if self.period else None
        return BudgetBasisDialog(self.conn, budget_id=self.budget_id, as_of=as_of,
                                 ok_text="Send to Retirement Planner", parent=self)

    def _send_to_retirement(self) -> None:
        dialog = self.make_basis_dialog()
        try:
            if not self._run_dialog(dialog):
                return
            basis = dialog.accepted_basis()
        finally:
            dialog.deleteLater()
        if basis is None or basis.annual_cents <= 0:
            self.say("Nothing to send: the exclusions account for the whole window.")
            return
        self.say(f"Sent {fmt_cents(basis.annual_cents)} a year to the Retirement "
                 f"Planner, in {basis.basis_year} dollars, un-inflated. The planner's "
                 f"own Apply is what writes a plan.")
        self.basis_ready.emit(basis)
