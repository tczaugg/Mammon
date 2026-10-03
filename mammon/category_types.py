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
# The third value a *picker* can ask for: neither side filtered out. It is not a
# classification a category can carry -- every category is income or expense --
# only a scope a caller selects (see :func:`top_level_categories`).
BOTH = "both"

# Income before expenses wherever categories are ordered by type.
TYPE_ORDER = {INCOME: 0, EXPENSE: 1}

__all__ = [
    "INCOME",
    "EXPENSE",
    "BOTH",
    "TYPE_ORDER",
    "classify",
    "net_by_category",
    "classify_categories",
    "category_type",
    "persist_types",
    "group_by_type",
    "top_level_categories",
    "category_forest",
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


def top_level_categories(conn, kind: str = BOTH, *,
                         include_hidden: bool = False) -> list[dict]:
    """The TOP-LEVEL categories of the requested ``kind`` as ``{'id', 'name',
    'type'}``, sorted case-insensitively by name.

    This is the one source of truth behind every report's category picker
    (``ui.report_filters.category_picker_names``). ``kind`` is :data:`INCOME`,
    :data:`EXPENSE` or :data:`BOTH`.

    Two properties the pickers depend on, each fixing a real defect:

    * It reads the ledger's own category TREE, never a report aggregation. The
      chart pickers used to list `reports.spending_by_category`, which sums only
      money OUT, so every income category was missing by construction and a
      category with no activity in the shown range vanished from the list --
      re-dating a report made a tick disappear. Here a zero-activity category is
      still offered.
    * A top level is classified by the ROLLED-UP net of its whole subtree, the
      same rule :func:`mammon.reports.itemized.itemize_tree` uses to decide which
      section a top level lands in. Classifying on the parent's own rows alone
      would call "Income" an expense whenever the money sits on Income:Salary,
      and the picker would then disagree with the report it filters.

    A persisted ``categories.type`` wins over the derived label (that column is
    the declaration hook, §5.9c), so a genuinely-income category that has never
    been used can still be declared income rather than defaulting to expense.
    """
    if kind not in (INCOME, EXPENSE, BOTH):
        raise ValueError(f"unknown category kind: {kind!r}")
    rows = conn.execute(
        "SELECT id, name, parent_id, type, hidden FROM categories").fetchall()
    nets = net_by_category(conn)
    kids: dict[Optional[int], list] = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)

    def rolled(cat_id: int) -> int:
        """Net signed cents for a category and every descendant. Hidden children
        still count -- hiding a sub-category does not change what the parent is."""
        total = nets.get(int(cat_id), 0)
        for kid in kids.get(cat_id, ()):
            total += rolled(kid["id"])
        return total

    out: list[dict] = []
    for r in kids.get(None, ()):
        if r["hidden"] and not include_hidden:
            continue
        stored = r["type"] if r["type"] in (INCOME, EXPENSE) else None
        typ = stored or classify(rolled(r["id"]))
        if kind != BOTH and typ != kind:
            continue
        out.append({"id": int(r["id"]), "name": r["name"], "type": typ})
    out.sort(key=lambda d: d["name"].lower())
    return out


def category_forest(conn, kind: str = BOTH, *,
                    include_hidden: bool = False) -> list[dict]:
    """The whole category HIERARCHY the report picker offers, scoped to ``kind``.

    Returns nested ``{'id', 'name', 'type', 'children': [...]}`` dicts: the
    top-level categories of the requested kind (exactly what
    :func:`top_level_categories` answers, same order and same classification
    rule) with every descendant hung underneath, each level sorted
    case-insensitively by name. ``type`` is carried on the top levels only --
    income-vs-expense is a property of the SIDE of the ledger a top level sits
    on, and a sub-category inherits its parent's side by construction.

    The scope is applied at the top level only, which is the whole of rule 2 of
    the picker: a subtree is offered only under a top-level category of the
    requested kind, so an ``income`` picker can never smuggle in an expense
    sub-category and the tree agrees with the flat list it replaced.

    The hierarchy walk is :func:`mammon.ledger.category_children`, so the UI
    layer never issues a hierarchy query of its own (the picker is a pure
    projection of this).
    """
    tops = top_level_categories(conn, kind, include_hidden=include_hidden)

    def walk(parent_id: int) -> list[dict]:
        return [{"id": int(c["id"]), "name": c["name"], "type": None,
                 "children": walk(int(c["id"]))}
                for c in ledger.category_children(conn, parent_id,
                                                  include_hidden=include_hidden)]

    return [{"id": t["id"], "name": t["name"], "type": t["type"],
             "children": walk(t["id"])} for t in tops]


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
