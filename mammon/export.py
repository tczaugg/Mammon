"""mammon.export -- the whole ledger out, in open formats (roadmap item 8).

The ledger is one SQLite file the user owns, but owning a file is not the same
as being able to leave with the data: another program has to be able to read
it. Three shapes, for three needs:

* **QIF** -- the format every personal-finance program reads, and the one this
  app's own importer was built on. Accounts, categories, every cash and
  investment transaction (splits, transfers on both sides, cleared flags,
  check numbers), the security list and the price history. The test of a
  complete QIF export is the round trip: exporting a ledger and importing the
  file into an empty database reproduces every balance and holding
  (``test_export.py``). Tags travel too, as Quicken writes them: a
  ``!Type:Tag`` master with descriptions, and a ``/Tag`` suffix on the category
  of a transaction or of an individual split leg. The format holds ONE tag per
  line -- no line in 27 years of Quicken's own exports carries two -- so a row
  with several keeps only the first. What QIF cannot carry -- the other tags on
  such a row, tag colors, loan setups, scheduled payments, learned rules, the
  exact ratio of an odd stock split -- stays behind, and the JSON export exists
  for that.

  **The migration is one-way, by decision.** Every record shape here was
  verified against Intuit's QIF specification and against 27 years of
  Quicken's own exports, and the file is correct. Quicken still cannot read it
  back faithfully: its QIF import applies transfer detection but not inside a
  split, so it lifts the transfer line out of a split transaction and files it
  separately -- a paycheck loses everything but its 401(k) deferral, a
  mortgage payment everything but its principal. That is Quicken's limitation,
  it is longstanding and not ours to work around, and the export does not
  contort the format to suit it. Bring a ledger to Mammon; do not plan on
  taking it back to Quicken.
* **JSON** -- every table, every row, lossless: the schema version and a
  dictionary of tables. Nothing is interpreted, so nothing can be lost. The
  identifying columns (account numbers, bank URLs, download settings) are
  blanked unless asked for, since "share the ledger with my son" is the
  common case and "move to another machine" the rare one.
* **CSV** -- one register per account, running balance included, for a
  spreadsheet.

Every function here is a read. Dates go out as ``MM/DD/YYYY`` in QIF (what
Quicken and the importer both accept) and ISO elsewhere; money as plain
decimal dollars.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from mammon import investments, ledger

# What a JSON export blanks unless the user asks to keep it: everything the MCP
# server never returns either.
SENSITIVE_COLUMNS = {
    "accounts": ("account_number", "url", "download_config", "download_script"),
}

# Mammon account types -> the QIF account-list ``T`` value and the section header.
_QIF_TYPE = {"checking": "Bank", "savings": "Bank", "credit": "CCard", "cash": "Cash",
             "asset": "Oth A", "liability": "Oth L", "investment": "Invst"}
_QIF_SECURITY_TYPE = {"stock": "Stock", "fund": "Mutual Fund", "etf": "ETF", "bond": "Bond",
                      "option": "Option", "cd": "CD", "other": "Other"}


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def qif_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(m):02d}/{int(d):02d}/{int(y):04d}"


def money(cents: Optional[int]) -> str:
    """Signed dollars with two decimals and no thousands separator."""
    c = int(cents or 0)
    sign = "-" if c < 0 else ""
    return f"{sign}{abs(c) // 100}.{abs(c) % 100:02d}"


def _dec(text) -> str:
    """A stored Decimal text as QIF/CSV wants it: no exponent, trailing zeros
    trimmed, never empty when a value exists."""
    if text in (None, ""):
        return ""
    d = Decimal(str(text))
    s = format(d.normalize(), "f") if d != 0 else "0"
    return s


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "account"


def _in_range(date: str, start: Optional[str], end: Optional[str]) -> bool:
    return (start is None or date >= start) and (end is None or date <= end)


def _accounts(conn, account_ids: Optional[Iterable[int]]) -> list:
    rows = ledger.list_accounts(conn, include_closed=True, include_hidden=True)
    if account_ids is None:
        return list(rows)
    wanted = {int(a) for a in account_ids}
    return [r for r in rows if int(r["id"]) in wanted]


# ---------------------------------------------------------------------------
# QIF
# ---------------------------------------------------------------------------
def _cleared_code(row) -> str:
    if row["reconciled"]:
        return "X"
    if row["cleared"]:
        return "*"
    return ""


def _category_lines(conn) -> list:
    out = ["!Type:Cat"]
    for c in conn.execute("SELECT id, type FROM categories ORDER BY id"):
        path = ledger.category_path(conn, c["id"])
        if not path:
            continue
        out.append(f"N{path}")
        out.append("I" if (c["type"] or "") == "income" else "E")
        out.append("^")
    return out


def _tag_lines(conn) -> list:
    """The ``!Type:Tag`` master, Quicken's own N/D shape (spec: "Items for a
    Class List"). Written whole, including tags no exported transaction uses:
    a tag the user defined still exists, and its description is the only place
    its meaning is recorded."""
    rows = conn.execute(
        "SELECT name, description FROM tags ORDER BY name").fetchall()
    if not rows:
        return []
    out = ["!Type:Tag"]
    for r in rows:
        out.append(f"N{r['name']}")
        if r["description"]:
            out.append(f"D{r['description']}")
        out.append("^")
    return out


def _security_rows(conn, account_ids: Optional[Iterable[int]]) -> list:
    """(name, symbol, type) for every security the exported accounts have
    traded, joined with what the securities table says about it."""
    info = {r["symbol"]: r for r in conn.execute("SELECT * FROM securities")}
    seen: list = []
    for a in _accounts(conn, account_ids):
        if a["type"] != "investment":
            continue
        for sym in investments.symbols_used(conn, int(a["id"])):
            if sym not in seen:
                seen.append(sym)
    out = []
    for sym in seen:
        r = info.get(sym)
        # N must be the text the transactions' Y lines carry: the importer keys
        # a !Type:Prices quote by the security's NAME through this list, so a
        # descriptive name here would file the prices under a name no
        # transaction uses. The long name lives in the JSON export.
        typ = _QIF_SECURITY_TYPE.get((r["sec_type"] if r is not None else None) or "", "")
        out.append((sym, sym, typ))
    return out


def _opening_date(conn, acct) -> str:
    """The date an opening balance is dated: the account's own opening date,
    else the day before its first transaction, else today."""
    if acct["opening_date"]:
        return acct["opening_date"]
    first = conn.execute("SELECT MIN(date) FROM transactions WHERE account_id=?",
                         (int(acct["id"]),)).fetchone()[0]
    if first:
        return (_dt.date.fromisoformat(first) - _dt.timedelta(days=1)).isoformat()
    return _dt.date.today().isoformat()


def _cash_txn_lines(conn, row, names: dict) -> list:
    # Quicken writes U before T, the same amount in both (U is the amount in
    # the account's own currency). Its own exports carry both on almost every
    # record, so we write both rather than T alone.
    out = [f"D{qif_date(row['date'])}", f"U{money(row['amount'])}",
           f"T{money(row['amount'])}"]
    code = _cleared_code(row)
    if code:
        out.append(f"C{code}")
    if row["num"]:
        out.append(f"N{row['num']}")
    if row["payee"]:
        out.append(f"P{row['payee']}")
    if row["memo"]:
        out.append(f"M{row['memo']}")
    splits = ledger.get_splits(conn, row["id"])
    if splits:
        # Quicken echoes the first leg's label on L, bracketed account and all
        # (verified against its own exports); the S lines carry the posting.
        out.append(f"L{_tagged(splits[0]['category_label'], splits[0]['tag'])}")
        for s in splits:
            out.append(f"S{_tagged(s['category_label'], s['tag'])}")
            if s["memo"]:
                out.append(f"E{s['memo']}")
            out.append(f"${money(s['amount'])}")
    elif row["transfer_account_id"] is not None:
        out.append(f"L[{names.get(int(row['transfer_account_id']), 'Transfer')}]")
    else:
        path = ledger.category_path(conn, row["category_id"])
        if path:
            out.append(f"L{_tagged(path, (row['tag'] or '').split(',')[0].strip())}")
    out.append("^")
    return out


def _tagged(label: str, tag: str) -> str:
    """A QIF category field with its tag suffix: ``Category:Sub/Tag``.

    A bracketed ``[Account]`` transfer target takes no tag -- the slash would be
    read back as part of the account name."""
    if not tag or (label.startswith("[") and label.endswith("]")):
        return label
    return f"{label}/{tag}"


def _invst_cash_lines(conn, row, names: dict) -> list:
    """A cash row on an INVESTMENT account, in the shape Quicken's own exports
    use inside a ``!Type:Invst`` section (verified against them): an action
    line, ``U`` and ``T``, the payee, the bracketed counter-account, and a
    ``$`` amount line --

        D1/ 8'15 / NXIn / PMDA Information Systems / U650.00 / T650.00 /
        L[Anytown CU Ck] / $650.00

    A transfer leg is ``XIn``/``XOut``; anything else is ``Cash`` with its
    category. Writing a BANK-style record here instead -- no action line at
    all, which is what this export did -- is malformed for an investment
    section, and it is how the 401(k) side of a paycheck came back as a bare
    transfer with the rest of the split gone."""
    amount = int(row["amount"] or 0)
    transfer = row["transfer_account_id"] is not None
    out = [f"D{qif_date(row['date'])}"]
    out.append(f"N{'XIn' if amount >= 0 else 'XOut'}" if transfer else "NCash")
    if row["payee"]:
        out.append(f"P{row['payee']}")
    shown = abs(amount) if transfer else amount
    out += [f"U{money(shown)}", f"T{money(shown)}"]
    code = _cleared_code(row)
    if code:
        out.append(f"C{code}")
    if row["memo"]:
        out.append(f"M{row['memo']}")
    if transfer:
        out.append(f"L[{names.get(int(row['transfer_account_id']), 'Transfer')}]")
        out.append(f"${money(shown)}")
    else:
        splits = ledger.get_splits(conn, row["id"])
        path = (splits[0]["category_label"] if splits
                else ledger.category_path(conn, row["category_id"]))
        if path:
            out.append(f"L{path}")
    out.append("^")
    return out


def _invst_amount(row) -> str:
    """Quicken writes a Buy/Sell/Div amount as a positive magnitude (the action
    says which way the cash goes) and a generic Cash line signed; the importer
    detects either convention, but Quicken itself expects its own."""
    a = (row["action"] or "").strip().lower().replace(" ", "")
    amount = int(row["amount"] or 0)
    if a == "cash" or not investments.is_known_action(a):
        return money(amount)
    return money(abs(amount))


def _invst_txn_lines(row, names: dict) -> list:
    out = [f"D{qif_date(row['date'])}", f"N{row['action']}"]
    if row["symbol"]:
        out.append(f"Y{row['symbol']}")
    a = (row["action"] or "").strip().lower().replace(" ", "")
    if a in investments._SPLIT_ACTIONS:
        # Quicken's encoding: new shares per TEN old. An odd ratio (4:3) is
        # rounded here; the JSON export keeps the exact pair.
        f = investments.split_factor(row)
        if f is not None:
            out.append(f"Q{_dec(Decimal(f.numerator * 10) / Decimal(f.denominator))}")
    else:
        if row["quantity"]:
            out.append(f"Q{_dec(row['quantity'])}")
        if row["price"]:
            out.append(f"I{_dec(row['price'])}")
        if row["amount"]:
            out.append(f"T{_invst_amount(row)}")
        if row["commission"]:
            out.append(f"O{money(abs(int(row['commission'])))}")
    if row["memo"]:
        out.append(f"M{row['memo']}")
    if row["transfer_account_id"] is not None:
        out.append(f"L[{names.get(int(row['transfer_account_id']), 'Transfer')}]")
    out.append("^")
    return out


def _qif_document(conn, accounts, names: dict, start: Optional[str], end: Optional[str],
                  *, opening: bool, include_empty: bool) -> tuple:
    """The lines of one QIF file over ``accounts`` for ``[start, end]``, and
    its counts. Every file stands alone -- the account list, the categories
    and the securities lead it -- so a set of yearly files can be imported in
    any order except for the opening balances, which ``opening`` writes (the
    first file). ``include_empty`` keeps an account's section even with
    nothing in the range: one whole-ledger file lists every account; a
    year's file only the accounts with activity that year."""
    # The tag master leads the file, where Quicken puts it (line 1 of every one
    # of its own exports of this ledger).
    lines: list = _tag_lines(conn)
    lines += ["!Option:AutoSwitch", "!Account"]
    for a in accounts:
        lines += [f"N{a['name']}", f"T{_QIF_TYPE.get(a['type'], 'Bank')}", "^"]
    lines.append("!Clear:AutoSwitch")
    cat_lines = _category_lines(conn)
    lines += cat_lines
    securities = _security_rows(conn, [int(a["id"]) for a in accounts])
    if securities:
        lines.append("!Type:Security")
        for name, sym, typ in securities:
            lines += [f"N{name}", f"S{sym}"]
            if typ:
                lines.append(f"T{typ}")
            lines.append("^")
    n_prices = 0
    price_lines: list = []
    for _name, sym, _typ in securities:
        for date, close in investments.price_history(conn, sym):
            if _in_range(date, start, end):
                price_lines += [f'"{sym}",{_dec(close)},"{qif_date(date)}"', "^"]
                n_prices += 1
    if price_lines:
        lines.append("!Type:Prices")
        lines += price_lines
    n_txn = n_inv = 0
    for a in accounts:
        aid = int(a["id"])
        section: list = []
        if a["type"] == "investment":
            for t in investments.list_investment_txns(conn, aid):
                if _in_range(t["date"], start, end):
                    section += _invst_txn_lines(t, names)
                    n_inv += 1
        elif a["opening_balance"] and opening:
            # Quicken's convention for an opening balance: a transfer to the
            # account itself payee'd "Opening Balance", first in the section.
            # The importer adopts it as the opening balance. Written once --
            # never into a slice, nor into any yearly file but the first.
            section += [f"D{qif_date(_opening_date(conn, a))}",
                        f"T{money(a['opening_balance'])}", "POpening Balance",
                        f"L[{a['name']}]", "^"]
        for r in conn.execute(
                "SELECT * FROM transactions WHERE account_id=? AND scheduled=0 "
                "ORDER BY date, id", (aid,)):
            if _in_range(r["date"], start, end):
                section += (_invst_cash_lines(conn, r, names) if a["type"] == "investment"
                            else _cash_txn_lines(conn, r, names))
                n_txn += 1
        if section or include_empty:
            lines += ["!Account", f"N{a['name']}", f"T{_QIF_TYPE.get(a['type'], 'Bank')}",
                      "^", f"!Type:{_QIF_TYPE.get(a['type'], 'Bank')}"] + section
    counts = {"accounts": len(accounts), "transactions": n_txn,
              "investment_transactions": n_inv,
              "categories": sum(1 for l in cat_lines if l.startswith("N")),
              "securities": len(securities), "prices": n_prices}
    return lines, counts


def _years(conn, accounts, start: Optional[str], end: Optional[str]) -> list:
    """The years in which the accounts have posted activity within the range."""
    years: set = set()
    for a in accounts:
        aid = int(a["id"])
        for table, extra in (("transactions", " AND scheduled=0"), ("investment_transactions", "")):
            for r in conn.execute(
                    f"SELECT DISTINCT substr(date,1,4) AS y FROM {table} "
                    f"WHERE account_id=?{extra}", (aid,)):
                y = r["y"]
                if y and _in_range(f"{y}-06-30", start[:4] + "-01-01" if start else None,
                                   end[:4] + "-12-31" if end else None):
                    years.add(y)
    return sorted(years)


def _year_stem(path) -> str:
    """Where yearly files go: ``<stem>-YYYY.qif`` beside the chosen file, or
    ``ledger-YYYY.qif`` inside a chosen folder."""
    p = Path(path)
    if p.is_dir() or str(path).endswith(("/", "\\")):
        return str(p / "ledger")
    if p.suffix.lower() == ".qif":
        return str(p.with_suffix(""))
    return str(p)


def export_qif(conn, path, *, account_ids: Optional[Iterable[int]] = None,
               start: Optional[str] = None, end: Optional[str] = None,
               by_year: bool = False) -> dict:
    """Write the ledger (or ``account_ids``, or the transactions dated in
    ``[start, end]``) as one QIF file -- or, with ``by_year``, as one file per
    year of activity, ``<stem>-YYYY.qif``, each complete in itself (account
    list, categories, securities, that year's prices and transactions) and
    the first carrying the opening balances -- a fallback for a receiving
    program that balks at one large file (Quicken could not WRITE its whole
    ledger as one QIF and had to be exported a year at a time; whether it
    reads one is for the user to find out). Returns counts:
    accounts, transactions, investment_transactions, categories, securities,
    prices, plus ``files`` (paths written) and, by year, ``years``."""
    accounts = _accounts(conn, account_ids)
    names = {int(a["id"]): a["name"] for a in
             ledger.list_accounts(conn, include_closed=True, include_hidden=True)}
    if not by_year:
        lines, counts = _qif_document(conn, accounts, names, start, end,
                                      opening=start is None, include_empty=True)
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        counts["files"] = [str(path)]
        return counts
    stem = _year_stem(path)
    years = _years(conn, accounts, start, end)
    totals = {"accounts": len(accounts), "transactions": 0, "investment_transactions": 0,
              "categories": 0, "securities": 0, "prices": 0, "files": [], "years": years}
    for i, year in enumerate(years):
        y0 = max(f"{year}-01-01", start or "")
        y1 = min(f"{year}-12-31", end or "9999-12-31")
        lines, c = _qif_document(conn, accounts, names, y0, y1,
                                 opening=(i == 0 and start is None), include_empty=False)
        out = Path(f"{stem}-{year}.qif")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        totals["files"].append(str(out))
        for k in ("transactions", "investment_transactions", "prices"):
            totals[k] += c[k]
        totals["categories"], totals["securities"] = c["categories"], c["securities"]
    return totals


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def _tables(conn) -> list:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]


def ledger_dict(conn, *, include_sensitive: bool = False) -> dict:
    """Every table as a list of row dicts, under the schema version, so the
    file says exactly which shape it holds."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables: dict = {}
    for name in _tables(conn):
        blank = () if include_sensitive else SENSITIVE_COLUMNS.get(name, ())
        rows = []
        for r in conn.execute(f'SELECT * FROM "{name}"'):
            d = dict(r)
            for col in blank:
                if col in d:
                    d[col] = None
            rows.append(d)
        tables[name] = rows
    return {"format": "mammon-ledger", "schema_version": int(version),
            "exported_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "sensitive_included": bool(include_sensitive), "tables": tables}


def export_json(conn, path, *, include_sensitive: bool = False) -> dict:
    """Write :func:`ledger_dict` to ``path``. Returns a count of rows per table."""
    data = ledger_dict(conn, include_sensitive=include_sensitive)
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return {name: len(rows) for name, rows in data["tables"].items()}


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
CASH_HEADERS = ["Date", "Num", "Payee", "Category", "Memo", "Tag", "Clr", "Amount",
                "Balance", "Splits"]
INVST_HEADERS = ["Date", "Action", "Security", "Quantity", "Price", "Amount",
                 "Commission", "Memo", "Transfer"]


def export_csv(conn, account_id: int, path, *, start: Optional[str] = None,
               end: Optional[str] = None) -> int:
    """One account's register as CSV -- the running balance is the balance on
    that date whatever the range, since it is computed over every row and only
    the rows in range are written. Returns the number of rows written."""
    acct = ledger.get_account(conn, account_id)
    if acct is None:
        raise KeyError(f"no account {account_id}")
    names = {int(a["id"]): a["name"] for a in
             ledger.list_accounts(conn, include_closed=True, include_hidden=True)}
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if acct["type"] == "investment":
            w.writerow(INVST_HEADERS)
            for t in investments.list_investment_txns(conn, account_id):
                if not _in_range(t["date"], start, end):
                    continue
                w.writerow([t["date"], t["action"], t["symbol"] or "",
                            investments.split_display(t) if (t["action"] or "").lower().replace(" ", "")
                            in investments._SPLIT_ACTIONS else _dec(t["quantity"]),
                            _dec(t["price"]), money(t["amount"]) if t["amount"] else "",
                            money(t["commission"]) if t["commission"] else "", t["memo"] or "",
                            names.get(int(t["transfer_account_id"]), "")
                            if t["transfer_account_id"] is not None else ""])
                n += 1
            return n
        w.writerow(CASH_HEADERS)
        for r in ledger.register_rows(conn, account_id):
            if r.get("scheduled") or not _in_range(r["date"], start, end):
                continue
            splits = ledger.get_splits(conn, r["id"]) if r["is_split"] else []
            w.writerow([r["date"], r["num"] or "", r["payee"] or "", r["category_label"],
                        r["memo"] or "", r.get("tag") or "", _cleared_code(r),
                        money(r["amount"]), money(r["balance"]),
                        "; ".join(f"{s['category_label']} {money(s['amount'])}"
                                  + (f" ({s['memo']})" if s["memo"] else "")
                                  for s in splits)])
            n += 1
    return n


def export_csv_all(conn, folder, *, account_ids: Optional[Iterable[int]] = None,
                   start: Optional[str] = None, end: Optional[str] = None) -> list:
    """One CSV per account into ``folder`` (created if needed), named after
    the account. Returns the paths written."""
    out = []
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for a in _accounts(conn, account_ids):
        p = folder / f"{_safe_name(a['name'])}.csv"
        export_csv(conn, int(a["id"]), p, start=start, end=end)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# one entry point, for the dialog and the command line
# ---------------------------------------------------------------------------
FORMATS = ("qif", "json", "csv")


def run(conn, fmt: str, path, *, account_ids: Optional[Iterable[int]] = None,
        start: Optional[str] = None, end: Optional[str] = None,
        include_sensitive: bool = False, by_year: bool = False) -> str:
    """Export in ``fmt`` to ``path`` (a file for qif/json, a folder for csv)
    and return a one-line summary for the user."""
    fmt = (fmt or "").lower()
    if fmt == "qif":
        c = export_qif(conn, path, account_ids=account_ids, start=start, end=end,
                       by_year=by_year)
        if by_year:
            files = c["files"]
            where = (f"{len(files)} yearly files, {Path(files[0]).name} to "
                     f"{Path(files[-1]).name}" if files else "no yearly files (nothing posted)")
        else:
            where = str(path)
        return (f"Wrote {where}: {c['accounts']} accounts, {c['transactions']} transactions, "
                f"{c['investment_transactions']} investment transactions, "
                f"{c['categories']} categories, {c['securities']} securities, "
                f"{c['prices']} prices.")
    if fmt == "json":
        c = export_json(conn, path, include_sensitive=include_sensitive)
        return (f"Wrote {path}: {len(c)} tables, {sum(c.values())} rows"
                + ("" if include_sensitive else
                   " (account numbers and login details left out)") + ".")
    if fmt == "csv":
        paths = export_csv_all(conn, path, account_ids=account_ids, start=start, end=end)
        return f"Wrote {len(paths)} register files into {path}."
    raise ValueError(f"unknown export format {fmt!r}; one of {FORMATS}")


def _default_db() -> Path:
    # Anchored to the install root, never the working directory (see app._resolve_db).
    return Path(__file__).resolve().parent.parent / "data" / "mammon.db"


def main(argv=None) -> int:
    from mammon import db
    ap = argparse.ArgumentParser(
        prog="python -m mammon.export",
        description="Export the ledger as QIF, JSON (every table) or CSV (one file per account).")
    ap.add_argument("--db", default=None, help="database file (default: data/mammon.db)")
    ap.add_argument("--format", choices=FORMATS, required=True)
    ap.add_argument("--out", required=True, help="output file (qif/json) or folder (csv)")
    ap.add_argument("--start", default=None, help="first date, YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="last date, YYYY-MM-DD")
    ap.add_argument("--account", action="append", default=None,
                    help="account name to include (repeatable; default all)")
    ap.add_argument("--include-sensitive", action="store_true",
                    help="JSON: keep account numbers, bank URLs and download settings")
    ap.add_argument("--by-year", action="store_true",
                    help="QIF: one file per year, <out stem>-YYYY.qif, if the "
                         "receiving program balks at one large file")
    args = ap.parse_args(argv)
    path = Path(args.db) if args.db else _default_db()
    conn = db.connect(str(path))
    try:
        ids = None
        if args.account:
            by_name = {a["name"].lower(): int(a["id"]) for a in
                       ledger.list_accounts(conn, include_closed=True, include_hidden=True)}
            ids = []
            for name in args.account:
                if name.lower() not in by_name:
                    ap.error(f"no account named {name!r}")
                ids.append(by_name[name.lower()])
        print(run(conn, args.format, args.out, account_ids=ids, start=args.start,
                  end=args.end, include_sensitive=args.include_sensitive,
                  by_year=args.by_year))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
