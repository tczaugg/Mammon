"""classic income-vs-expense classification of categories.

Every category is one of ``income`` or ``expense`` -- the same split Quicken
draws so reports and charts can list income first, then expenses. Mammon derives
the type from the *data* rather than asking the user to declare it: a category
whose transactions net POSITIVE (money predominantly flowing in -- Salary,
Interest, Refunds) is income; one that nets NEGATIVE (money flowing out --
Groceries, Rent) is expense.

The derivation mirrors :mod:`mammon.reports.spending` exactly on which rows count:
transfers are excluded (``transfer_account_id IS NULL``), a split transaction is
represented by its split lines (not its NULL-category parent), and a plain
transaction by its own signed amount. Only the SIGN is kept here (spending.py
throws away positive amounts; we need both directions).

The dormant ``categories.type`` column is the storage hook. :func:`persist_types`
writes the derived label there so the value is available without a re-scan;
:func:`category_type` reads that stored label when present and otherwise derives
on the fly. Reports/charts should call :func:`group_by_type` to get categories
already ordered income-first.
"""
from __future__ import annotations

from typing import Optional

from . import ledger

INCOME = "income"
EXPENSE = "expense"

# Income before expenses wherever categories are ordered by type.
TYPE_ORDER = {INCOME: 0, EXPENSE: 1}

__all__ = [
    "INCOME",
    "EXPENSE",
    "TYPE_ORDER",
    "classify",
    "net_by_category",
    "classify_categories",
    "category_type",
    "persist_types",
    "group_by_type",
]


def classify(net_cents: int) -> str:
    """Label a net signed-cents total: predominantly-positive => income,
    otherwise expense. A zero (or empty) net is treated as expense -- the
    conventional default for a category with no clear inflow."""
    return INCOME if int(net_cents) > 0 else EXPENSE


def net_by_category(conn) -> dict[int, int]:
    """``category_id -> net signed cents`` across all non-transfer activity.

    Split transactions contribute their split lines; plain transactions their
    own amount. Uncategorized rows (``category_id IS NULL``) are dropped.
    """
    txns = conn.execute(
        "SELECT id, category_id, amount FROM transactions "
        "WHERE transfer_account_id IS NULL"
    ).fetchall()
    if not txns:
        return {}

    ids = [t["id"] for t in txns]
    splits: dict[int, list] = {}
    for chunk_start in range(0, len(ids), 500):     # keep the IN() list bounded
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


def classify_categories(conn) -> dict[int, str]:
    """``category_id -> 'income'|'expense'`` for every category (hidden ones
    included). A category with no activity nets zero and classifies as expense."""
    nets = net_by_category(conn)
    out: dict[int, str] = {}
    for cat in ledger.list_categories(conn, include_hidden=True):
        out[cat["id"]] = classify(nets.get(cat["id"], 0))
    return out


def category_type(conn, category_id: int) -> str:
    """The income/expense label for one category: the stored ``type`` if it has
    been persisted, otherwise derived live from that category's net."""
    row = conn.execute(
        "SELECT type FROM categories WHERE id=?", (category_id,)
    ).fetchone()
    if row is not None and row["type"] in (INCOME, EXPENSE):
        return row["type"]
    return classify(net_by_category(conn).get(int(category_id), 0))


def persist_types(conn) -> int:
    """Write each category's derived income/expense label into ``categories.type``
    so callers need not re-scan the ledger. Returns the number of rows written.
    Commits."""
    types = classify_categories(conn)
    for cid, typ in types.items():
        conn.execute("UPDATE categories SET type=? WHERE id=?", (typ, cid))
    conn.commit()
    return len(types)


def group_by_type(conn, *, include_hidden: bool = False) -> dict[str, list[dict]]:
    """Categories grouped for reports/charts as ``{'income': [...], 'expense':
    [...]}`` -- income first, then expense, each list ordered by path. Every row
    is ``{'id', 'path', 'type', 'net_cents'}``. This is the single helper the
    report and chart code should call to lay income out before expenses.
    """
    nets = net_by_category(conn)
    grouped: dict[str, list[dict]] = {INCOME: [], EXPENSE: []}
    for cat in ledger.list_categories(conn, include_hidden=include_hidden):
        net = nets.get(cat["id"], 0)
        typ = classify(net)
        grouped[typ].append(
            {"id": cat["id"], "path": cat["path"], "type": typ, "net_cents": net})
    # list_categories already sorts by path; preserve that within each group.
    return grouped
