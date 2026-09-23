"""mammon.reports.itemized -- every category with its REAL signed net (SRD 5.9).

Where :mod:`mammon.reports.spending` reports money OUT only (expenses as positive
magnitudes), this report lists ALL categories together -- income and expense --
each carrying its actual SIGNED net over the period. Income categories net
positive (shown without a sign, colored green by the GUI); expense categories
net negative (shown with a leading minus, colored red). Totals sum the signed
values, so the grand total is the period's net cash flow across categories.

Ordering follows :mod:`mammon.category_types`: INCOME categories first, then
EXPENSE, using that module's data-derived classification so the two reports agree
on what is income vs expense. Only categories with activity in the period appear
(a category that nets exactly zero for the window is dropped). Splits are honored
exactly as the spending report does; uncategorized activity (no category) is not a
category and is not listed.

Transfer accounts follow, as their own section after income and expense: each
account money was transferred to/from is one row, labelled with its bracketed name
(e.g. ``[Vanguard 401k]``) and carrying the SIGNED net of the transfer legs whose
counterparty is that account over the period. A net-zero account is dropped like a
net-zero category, and the legs are summed once each (no double counting of the
mirror), so the grand total stays the period's true net cash flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from mammon import category_types
from mammon.category_types import EXPENSE, INCOME
from mammon.reports.spending import _fmt_dollars, _validate_date

TRANSFER = "transfer"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class ItemizedRow:
    category_id: Optional[int]  # None for a transfer row
    name: str                 # leaf name ("Salary") or bracket label ("[Savings]")
    path: str                 # full path ("Income:Salary") or "[Savings]"
    type: str                 # 'income' | 'expense' | 'transfer'
    net_cents: int            # SIGNED net over the period (may be negative)
    account_id: Optional[int] = None   # transfer counterparty (transfer rows only)


@dataclass
class ItemizedReport:
    start: str
    end: str
    account_ids: Optional[list]
    rows: list                # income rows first, then expense; each ItemizedRow
    total_cents: int          # SIGNED sum of every row's net_cents


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def itemize_by_category(conn, start: str, end: str,
                        account_ids: Optional[Iterable[int]] = None,
                        category_ids: Optional[Iterable[int]] = None) -> ItemizedReport:
    """Every category's SIGNED net over ``[start, end]`` (inclusive ISO dates),
    ordered income-first then expense.

    ``account_ids`` restricts the report to those accounts; ``None`` is all
    accounts and an explicit empty iterable selects none (an empty report).

    ``category_ids`` restricts the income/expense rows to that subset of
    categories; ``None`` includes every category (an explicit empty iterable
    drops them all). It matches each row by its own ``category_id``, so a
    parent and its sub-categories are selected independently. Transfer rows are
    NOT categories and are unaffected -- they follow ``account_ids`` alone.
    """
    _validate_date(start)
    _validate_date(end)
    acct_list = None if account_ids is None else [int(a) for a in account_ids]
    cat_filter = None if category_ids is None else {int(c) for c in category_ids}

    nets = _period_net_by_category(conn, start, end, acct_list)
    grouped = category_types.group_by_type(conn, include_hidden=True)

    rows: list[ItemizedRow] = []
    for typ in (INCOME, EXPENSE):                       # income before expense
        for cat in grouped[typ]:                        # already path-ordered
            if cat_filter is not None and cat["id"] not in cat_filter:
                continue                                # not in the chosen subset
            net = nets.get(cat["id"], 0)
            if net == 0:
                continue                                # no activity this period
            path = cat["path"]
            rows.append(ItemizedRow(
                category_id=cat["id"], name=path.split(":")[-1], path=path,
                type=typ, net_cents=net,
            ))

    # Transfer section: one row per counterparty account, signed net of its legs,
    # ordered by account name, after income and expense.
    tnets = _period_net_by_transfer_account(conn, start, end, acct_list)
    if tnets:
        names = {r["id"]: r["name"]
                 for r in conn.execute("SELECT id, name FROM accounts").fetchall()}
        for acct_id in sorted(tnets, key=lambda i: names.get(i, "")):
            net = tnets[acct_id]
            if net == 0:
                continue                                # nets flat this period
            label = f"[{names.get(acct_id, acct_id)}]"
            rows.append(ItemizedRow(
                category_id=None, name=label, path=label,
                type=TRANSFER, net_cents=net, account_id=acct_id,
            ))

    total = sum(r.net_cents for r in rows)
    return ItemizedReport(start=start, end=end, account_ids=acct_list,
                          rows=rows, total_cents=total)


def _period_net_by_category(conn, start: str, end: str,
                            acct_list: Optional[list[int]]) -> dict[int, int]:
    """``category_id -> net SIGNED cents`` over the window, honoring splits and
    excluding transfers. Uncategorized rows (``category_id IS NULL``) are dropped.
    Mirrors :func:`mammon.category_types.net_by_category` plus a date/account
    filter, so both directions of money are kept (unlike the spending report).
    """
    where = ["date >= ?", "date <= ?", "transfer_account_id IS NULL"]
    params: list = [start, end]
    if acct_list is not None:
        if not acct_list:
            return {}                       # no accounts selected -> nothing
        marks = ",".join("?" for _ in acct_list)
        where.append(f"account_id IN ({marks})")
        params.extend(acct_list)
    sql = "SELECT id, category_id, amount FROM transactions WHERE " + " AND ".join(where)
    txns = conn.execute(sql, params).fetchall()
    if not txns:
        return {}

    ids = [t["id"] for t in txns]
    splits: dict[int, list] = {}
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" for _ in chunk)
        for s in conn.execute(
            f"SELECT transaction_id, category_id, amount FROM splits "
            f"WHERE transaction_id IN ({marks})", chunk,
        ).fetchall():
            splits.setdefault(s["transaction_id"], []).append(s)

    totals: dict[Optional[int], int] = {}
    for t in txns:
        lines = splits.get(t["id"])
        if lines:                                   # split txn: use the split lines
            for s in lines:
                totals[s["category_id"]] = totals.get(s["category_id"], 0) + s["amount"]
        else:                                       # plain txn: use its own amount
            totals[t["category_id"]] = totals.get(t["category_id"], 0) + t["amount"]
    totals.pop(None, None)
    return {int(k): int(v) for k, v in totals.items()}


def _period_net_by_transfer_account(conn, start: str, end: str,
                                    acct_list: Optional[list[int]]) -> dict[int, int]:
    """``counterparty_account_id -> net SIGNED cents`` of the transfer legs whose
    other side is that account, over ``[start, end]``.

    Two disjoint sources are summed, each leg counted exactly once (no double
    counting of the mirror):

    * whole-transaction transfer legs -- ``transactions`` rows carrying a
      ``transfer_account_id`` (both sides of a :func:`ledger.create_transfer`
      pair, and the counter-account mirror a split transfer posts);
    * split transfer legs -- ``splits`` rows carrying a ``transfer_account_id``
      (the source side, which lives only in ``splits``), dated/scoped by their
      parent transaction.

    ``acct_list`` restricts by the leg's OWN account (its parent's, for splits),
    mirroring the category filter. Legs are grouped by the counterparty, so the
    signed amount reads from the rest of your accounts' perspective: money moved
    toward the account is negative, money received from it positive.
    """
    if acct_list is not None and not acct_list:
        return {}
    totals: dict[int, int] = {}

    # (1) Whole-transaction transfer legs.
    where = ["date >= ?", "date <= ?", "transfer_account_id IS NOT NULL"]
    params: list = [start, end]
    if acct_list is not None:
        marks = ",".join("?" for _ in acct_list)
        where.append(f"account_id IN ({marks})")
        params.extend(acct_list)
    sql = ("SELECT transfer_account_id AS acct, amount FROM transactions WHERE "
           + " AND ".join(where))
    for r in conn.execute(sql, params).fetchall():
        totals[r["acct"]] = totals.get(r["acct"], 0) + r["amount"]

    # (2) Split transfer legs (source side; only in splits, dated by the parent).
    where2 = ["t.date >= ?", "t.date <= ?", "s.transfer_account_id IS NOT NULL"]
    params2: list = [start, end]
    if acct_list is not None:
        marks = ",".join("?" for _ in acct_list)
        where2.append(f"t.account_id IN ({marks})")
        params2.extend(acct_list)
    sql2 = ("SELECT s.transfer_account_id AS acct, s.amount AS amount FROM splits s "
            "JOIN transactions t ON t.id = s.transaction_id WHERE "
            + " AND ".join(where2))
    for r in conn.execute(sql2, params2).fetchall():
        totals[r["acct"]] = totals.get(r["acct"], 0) + r["amount"]

    return {int(k): int(v) for k, v in totals.items() if k is not None}


# ---------------------------------------------------------------------------
# Plain-text renderer (signed; income section then expense)
# ---------------------------------------------------------------------------
def format_itemized_report(report: ItemizedReport, *, width: int = 48) -> str:
    """Render the itemized report as a right-aligned text table. Amounts are
    SIGNED -- negatives carry a leading minus, positives none -- and the total
    sums them, so it can read negative (net outflow) or positive (net inflow)."""
    rows = report.rows
    label_col = max(
        [len("Itemize by Category")]
        + [len(r.path) for r in rows]
        + [len("Total")]
    )
    amt_col = max(len(_fmt_dollars(report.total_cents)),
                  *([len(_fmt_dollars(r.net_cents)) for r in rows] or [0]))
    pad = max(width, label_col + 2 + amt_col)

    def line(label: str, cents: int) -> str:
        amt = _fmt_dollars(cents)
        return f"{label}{amt.rjust(pad - len(label))}"

    rule = "-" * pad
    lines = [f"Itemize by Category  {report.start} to {report.end}", rule]
    if not rows:
        lines.append("(no activity in this period)")
    for r in rows:
        lines.append(line(r.path, r.net_cents))
    lines.append(rule)
    lines.append(line("Total", report.total_cents))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Expandable tree (Quicken "Itemize by Category" register view)
# ---------------------------------------------------------------------------
# Unlike :func:`itemize_by_category` (a flat, income-first list of category nets
# used by the text renderer), :func:`itemize_tree` builds the hierarchical tree
# the GUI renders: INCOME / EXPENSES sections -> top-level categories -> sub-
# categories (recursively) -> the transactions themselves, with every level's
# amount rolled up from its descendants. It mirrors the reference screenshot at
# ``data/itemized_categories.png``. A parent that also carries its own direct
# transactions shows them under an "Other <path>" child (Quicken's convention),
# always ordered last after the real sub-categories. Net-zero subtrees WITH
# activity are kept (e.g. two offsetting transactions net 0 but still appear);
# only a category with no activity anywhere in its subtree is dropped.
@dataclass
class TxnLine:
    """One posting under a leaf/"Other" node -- a plain transaction or a single
    split line. ``amount_cents`` is the split line's amount for a split, else the
    transaction's own amount, so a category's lines sum to its direct net."""
    date: str
    account: str
    num: str
    description: str      # the payee
    memo: str
    cleared: str          # "R" reconciled, "c" cleared-not-reconciled, "" neither
    amount_cents: int


@dataclass
class TreeNode:
    kind: str                          # 'section'|'category'|'other'|'transfer'|'txn'
    label: str                         # first-column text (name, "Other X", or date)
    net_cents: int                     # rolled-up signed net (txn: its own amount)
    type: str = ""                     # 'income'|'expense'|'transfer' on grouping nodes
    category_id: Optional[int] = None
    account_id: Optional[int] = None   # transfer counterparty (transfer nodes)
    children: list = field(default_factory=list)
    line: Optional[TxnLine] = None     # populated only when kind == 'txn'


@dataclass
class ItemizedTree:
    start: str
    end: str
    account_ids: Optional[list]
    sections: list                     # TreeNode sections: income, expense, transfer
    total_cents: int                   # signed sum of the sections' nets


def _clr(cleared, reconciled) -> str:
    if reconciled:
        return "R"
    if cleared:
        return "c"
    return ""


def _line(t, amount, memo) -> TxnLine:
    return TxnLine(
        date=t["date"] or "", account=t["account"] or "", num=t["num"] or "",
        description=t["payee"] or "", memo=memo or "",
        cleared=_clr(t["cleared"], t["reconciled"]), amount_cents=int(amount),
    )


def _txn_nodes(lines: list) -> list:
    """Transaction leaf nodes, ordered by account then date (Quicken's
    ``Account/Date`` sort), so a single-account category reads chronologically."""
    ordered = sorted(lines, key=lambda l: (l.account, l.date))
    return [TreeNode(kind="txn", label=l.date, net_cents=l.amount_cents, line=l)
            for l in ordered]


def _period_lines_by_category(conn, start, end,
                              acct_list: Optional[list[int]]) -> dict[int, list]:
    """``category_id -> [TxnLine]`` over the window, honoring splits and excluding
    transfers -- the transaction-level counterpart of :func:`_period_net_by_category`
    (a category's lines sum to the same direct net). Uncategorized postings and
    split transfer legs (category_id NULL) are dropped."""
    where = ["t.date >= ?", "t.date <= ?", "t.transfer_account_id IS NULL"]
    params: list = [start, end]
    if acct_list is not None:
        if not acct_list:
            return {}
        marks = ",".join("?" for _ in acct_list)
        where.append(f"t.account_id IN ({marks})")
        params.extend(acct_list)
    sql = ("SELECT t.id AS id, a.name AS account, t.date AS date, t.num AS num, "
           "t.payee AS payee, t.memo AS memo, t.category_id AS category_id, "
           "t.amount AS amount, t.cleared AS cleared, t.reconciled AS reconciled "
           "FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE "
           + " AND ".join(where))
    txns = conn.execute(sql, params).fetchall()
    if not txns:
        return {}

    ids = [t["id"] for t in txns]
    splits: dict[int, list] = {}
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" for _ in chunk)
        for s in conn.execute(
            f"SELECT transaction_id, category_id, amount, memo FROM splits "
            f"WHERE transaction_id IN ({marks})", chunk,
        ).fetchall():
            splits.setdefault(s["transaction_id"], []).append(s)

    out: dict[int, list] = {}
    for t in txns:
        lines = splits.get(t["id"])
        if lines:                                   # split txn: one line per split
            for s in lines:
                cid = s["category_id"]
                if cid is None:                     # transfer leg / uncategorized
                    continue
                out.setdefault(int(cid), []).append(_line(t, s["amount"], s["memo"]))
        else:                                       # plain txn: its own amount
            cid = t["category_id"]
            if cid is None:
                continue
            out.setdefault(int(cid), []).append(_line(t, t["amount"], t["memo"]))
    return out


def _period_lines_by_transfer_account(conn, start, end,
                                      acct_list: Optional[list[int]]) -> dict[int, list]:
    """``counterparty_account_id -> [TxnLine]`` -- the transaction-level counterpart
    of :func:`_period_net_by_transfer_account` (whole-transaction transfer legs plus
    split transfer legs, each counted once)."""
    if acct_list is not None and not acct_list:
        return {}
    out: dict[int, list] = {}

    where = ["t.date >= ?", "t.date <= ?", "t.transfer_account_id IS NOT NULL"]
    params: list = [start, end]
    if acct_list is not None:
        marks = ",".join("?" for _ in acct_list)
        where.append(f"t.account_id IN ({marks})")
        params.extend(acct_list)
    sql = ("SELECT t.transfer_account_id AS acct, a.name AS account, t.date AS date, "
           "t.num AS num, t.payee AS payee, t.memo AS memo, t.amount AS amount, "
           "t.cleared AS cleared, t.reconciled AS reconciled "
           "FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE "
           + " AND ".join(where))
    for r in conn.execute(sql, params).fetchall():
        if r["acct"] is None:
            continue
        out.setdefault(int(r["acct"]), []).append(_line(r, r["amount"], r["memo"]))

    where2 = ["t.date >= ?", "t.date <= ?", "s.transfer_account_id IS NOT NULL"]
    params2: list = [start, end]
    if acct_list is not None:
        marks = ",".join("?" for _ in acct_list)
        where2.append(f"t.account_id IN ({marks})")
        params2.extend(acct_list)
    sql2 = ("SELECT s.transfer_account_id AS acct, a.name AS account, t.date AS date, "
            "t.num AS num, t.payee AS payee, s.memo AS memo, s.amount AS amount, "
            "t.cleared AS cleared, t.reconciled AS reconciled "
            "FROM splits s JOIN transactions t ON t.id = s.transaction_id "
            "JOIN accounts a ON a.id = t.account_id WHERE " + " AND ".join(where2))
    for r in conn.execute(sql2, params2).fetchall():
        if r["acct"] is None:
            continue
        out.setdefault(int(r["acct"]), []).append(_line(r, r["amount"], r["memo"]))
    return out


def itemize_tree(conn, start: str, end: str,
                 account_ids: Optional[Iterable[int]] = None,
                 top_level_names: Optional[Iterable[str]] = None,
                 category_ids: Optional[Iterable[int]] = None) -> ItemizedTree:
    """Hierarchical Itemize-by-Category report over ``[start, end]`` (inclusive).

    Returns an :class:`ItemizedTree` of INCOME / EXPENSES (and TRANSFERS) sections,
    each holding top-level category nodes that expand into their sub-categories and
    ultimately their transactions, with every amount rolled up. ``account_ids``
    restricts by account (``None`` = all). Transfers follow the account
    filter only. A top-level category is classified income vs expense by the sign
    of its rolled-up net.

    Two category filters, both ``None`` = all:

    * ``category_ids`` is the one the picker uses. It names EXACTLY the
      categories that may appear, at any depth, and it is applied per NODE --
      selecting ``Taxes:Federal`` while leaving ``Taxes:Property`` unticked drops
      the Property subtree and its money from the rolled-up totals, rather than
      keeping the whole Taxes subtree because its parent was eligible. A parent
      in the set but with an excluded child still contributes its own postings
      (the "Other <path>" line), which is what a partially-ticked parent means.
    * ``top_level_names`` is the older top-level-only form, kept for callers that
      still hold names. Both may be given; a category must satisfy both.
    """
    _validate_date(start)
    _validate_date(end)
    acct_list = None if account_ids is None else [int(a) for a in account_ids]
    name_filter = None if top_level_names is None else set(top_level_names)
    id_filter = None if category_ids is None else {int(c) for c in category_ids}

    lines_by_cat = _period_lines_by_category(conn, start, end, acct_list)

    cats: dict[int, dict] = {}
    kids: dict[Optional[int], list] = {}
    for r in conn.execute("SELECT id, name, parent_id FROM categories").fetchall():
        pid = None if r["parent_id"] is None else int(r["parent_id"])
        cats[int(r["id"])] = {"name": r["name"], "parent_id": pid}
        kids.setdefault(pid, []).append(int(r["id"]))

    path_cache: dict[int, str] = {}

    def path_of(cid: int) -> str:
        if cid in path_cache:
            return path_cache[cid]
        cat = cats[cid]
        p = (cat["name"] if cat["parent_id"] is None
             else path_of(cat["parent_id"]) + ":" + cat["name"])
        path_cache[cid] = p
        return p

    def sorted_kids(pid: Optional[int]) -> list:
        return sorted(kids.get(pid, []), key=lambda i: cats[i]["name"].lower())

    def build(cid: int) -> Optional[TreeNode]:
        if id_filter is not None and cid not in id_filter:
            return None                     # excluded: this node and its subtree
        own_lines = lines_by_cat.get(cid, [])
        child_nodes = [n for n in (build(k) for k in sorted_kids(cid)) if n]
        if not own_lines and not child_nodes:
            return None                     # no activity anywhere in this subtree
        own = sum(l.amount_cents for l in own_lines)
        total = own + sum(n.net_cents for n in child_nodes)
        node = TreeNode(kind="category", label=cats[cid]["name"], net_cents=total,
                        category_id=cid)
        if kids.get(cid):                   # has sub-categories in the tree
            node.children = list(child_nodes)
            if own_lines:                   # parent's own postings -> "Other <path>"
                node.children.append(TreeNode(
                    kind="other", label="Other " + path_of(cid), net_cents=own,
                    category_id=cid, children=_txn_nodes(own_lines)))
        else:                               # leaf -> the transactions themselves
            node.children = _txn_nodes(own_lines)
        return node

    income_tops: list = []
    expense_tops: list = []
    for cid in sorted_kids(None):
        if name_filter is not None and cats[cid]["name"] not in name_filter:
            continue
        node = build(cid)
        if node is None:
            continue
        bucket = income_tops if category_types.classify(node.net_cents) == INCOME \
            else expense_tops
        bucket.append(node)

    sections: list = []
    if income_tops:
        sections.append(TreeNode(kind="section", label="INCOME", type=INCOME,
                                 net_cents=sum(n.net_cents for n in income_tops),
                                 children=income_tops))
    if expense_tops:
        sections.append(TreeNode(kind="section", label="EXPENSES", type=EXPENSE,
                                 net_cents=sum(n.net_cents for n in expense_tops),
                                 children=expense_tops))

    tlines = _period_lines_by_transfer_account(conn, start, end, acct_list)
    if tlines:
        names = {r["id"]: r["name"]
                 for r in conn.execute("SELECT id, name FROM accounts").fetchall()}
        transfer_nodes = []
        for acct_id in sorted(tlines, key=lambda i: names.get(i, "")):
            lines = tlines[acct_id]
            transfer_nodes.append(TreeNode(
                kind="transfer", label=f"[{names.get(acct_id, acct_id)}]",
                net_cents=sum(l.amount_cents for l in lines), type=TRANSFER,
                account_id=acct_id, children=_txn_nodes(lines)))
        if transfer_nodes:
            sections.append(TreeNode(
                kind="section", label="TRANSFERS", type=TRANSFER,
                net_cents=sum(n.net_cents for n in transfer_nodes),
                children=transfer_nodes))

    total = sum(s.net_cents for s in sections)
    return ItemizedTree(start=start, end=end, account_ids=acct_list,
                        sections=sections, total_cents=total)
