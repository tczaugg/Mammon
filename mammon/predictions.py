"""mammon.predictions -- what will probably happen, read off recent history.

A reminder is something the user told the app. A prediction is something the
app noticed: a payee paid, or a payer received, at a steady interval and a
similar amount over the last few months, and therefore likely to recur. The
projection and the calendar show predictions beside the reminders, marked as
estimates, so the month ahead is not only the bills the user remembered to
define. A prediction can be dismissed per account and payee
(``prediction_dismissals``); the better answer, when the user knows the real
amount and date, is to make it a scheduled payment, which supersedes it.

One payee is not always one bill. A phone carrier paid for four family
members in rotation posts about weekly while each line is billed monthly, and
read as a single series it becomes a weekly payment of an arbitrary amount --
four times as many calendar events as there are bills. So a payee's history is
clustered into SUB-STREAMS (see ``split_substreams``) before any cadence is
fitted, and each sub-stream is fitted on its own.

Two things make a prediction trustworthy, and the code weighs both: three or
more occurrences (two are enough only when the amounts nearly match), and a
bank descriptor saying the payment is automatic -- AUTOPAY, ACH, RECURRING,
DIRECT DEP and the like -- since such a payment arrives whether or not anyone
remembers it. The descriptor is looked for on the rows themselves and in the
learned payee mappings, the raw text the renamer turned into this payee.

Reads only, except the dismissal table.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from mammon import ledger, rename_tree, scheduled

# Words in a bank descriptor or memo that mean "this happens by itself".
AUTOMATIC_HINTS = (
    "automatic", "autopay", "auto pay", "auto-pay", "autodraft", "auto draft",
    "recurring", "direct dep", "dirdep", "dir dep", "preauth", "pre-auth",
    "epay", "e-pay", "online pmt", "onlinepmt", "bill pay", "billpay", "payroll",
)
_ACH = re.compile(r"\bach\b")

# Within how many days of a predicted date an entered row for the same payee
# counts as that occurrence (so the prediction is not shown beside it).
ENTERED_WINDOW_DAYS = 3

# Predictions are a cash-flow question: only the accounts money is spent from.
# A loan register's principal mirrors recur just as steadily and would
# otherwise be "predicted" on the loan.
SPENDING_TYPES = ("checking", "savings", "credit", "cash")


def payee_key(payee) -> str:
    return scheduled._payee_key(payee)


def looks_automatic(*texts) -> bool:
    """True when any of ``texts`` carries an automatic-payment hint."""
    for t in texts:
        s = (t or "").lower()
        if not s:
            continue
        if _ACH.search(s) or any(h in s for h in AUTOMATIC_HINTS):
            return True
    return False


@dataclass
class Prediction:
    account_id: int
    payee: str
    key: str
    amount: int                       # signed cents, the latest occurrence's
    frequency: str
    last_date: str
    next_date: str                    # first expected date on/after today
    count: int
    automatic: bool
    category_id: Optional[int] = None
    varies: bool = False

    def entry(self) -> dict:
        """The shape ``scheduled.add_scheduled`` / the editor take, for turning
        the prediction into a definition."""
        return {"account_id": self.account_id, "payee": self.payee,
                "amount": self.amount, "frequency": self.frequency,
                "next_date": self.next_date, "category_id": self.category_id}


def _automatic_for(conn, payee: str, rows) -> bool:
    texts = [payee] + [r["memo"] for r in rows]
    # The raw descriptors this payee was renamed FROM: the rename tree holds
    # them (categorize's import_mappings never did -- its mapped_payee only
    # repeated the pattern, and it is no longer written at all).
    texts.extend(rename_tree.sources_for(conn, payee))
    return looks_automatic(*texts)


def predict_recurring(conn, today: str, *, account_ids: Optional[Iterable[int]] = None,
                      months_back: int = 6, min_count: int = 2,
                      include_dismissed: bool = False) -> list:
    """Payees on the accounts (every spending account when ``account_ids`` is
    None) that recurred over the last ``months_back`` months at a steady
    interval and a similar amount, each with its next expected date on/after
    ``today``. Six months, not three: a stretch of payroll trouble, with
    paychecks missed and made up close together, is outweighed by the regular
    months around it. Left out: pending rows, loan payments (the loan schedule
    owns them), payees an active definition already names on that account,
    and dismissed ones."""
    since = (_dt.date.fromisoformat(today)
             - _dt.timedelta(days=int(months_back * 30.44))).isoformat()
    sql = ("SELECT id, account_id, date, amount, payee, memo, category_id FROM transactions "
           "WHERE scheduled=0 AND date>=? AND date<=? AND payee IS NOT NULL AND payee<>'' "
           "AND id NOT IN (SELECT transaction_id FROM splits WHERE transfer_account_id IN "
           "               (SELECT account_id FROM loan_params))")
    params: list = [since, today]
    spending = {int(a["id"]) for a in ledger.list_accounts(conn, include_closed=False,
                                                            include_hidden=True)
                if (a["type"] or "") in SPENDING_TYPES}
    ids = sorted(spending) if account_ids is None else \
        [int(a) for a in account_ids if int(a) in spending]
    if not ids:
        return []
    sql += f" AND account_id IN ({','.join('?' for _ in ids)})"
    params += ids
    sql += " ORDER BY date, id"
    groups: dict = {}
    for r in conn.execute(sql, params):
        groups.setdefault((int(r["account_id"]), payee_key(r["payee"])), []).append(r)
    covered = {(int(d["account_id"]), payee_key(d["payee"]))
               for d in scheduled.list_scheduled(conn, active_only=True)}
    skip = set() if include_dismissed else {
        (int(d["account_id"]), d["payee_key"]) for d in dismissed(conn)}
    out = []
    for (aid, key), items in groups.items():
        if len(key) < 3 or (aid, key) in covered or (aid, key) in skip:
            continue
        out.extend(payee_predictions(conn, aid, key, items, today,
                                     min_count=max(2, min_count)))
    out.sort(key=lambda p: (p.next_date, p.account_id, p.payee.lower()))
    return out


def payee_predictions(conn, account_id: int, key: str, items, today: str, *,
                      min_count: int = 2) -> list:
    """Every prediction one payee's rows on one account support: normally one,
    and one per sub-stream when the payee is several bills at once (see
    ``split_substreams``).

    The split is taken only when it is clearly the better reading -- two or
    more sub-streams that EACH pass the same fit, amount-consistency and
    minimum-occurrence tests a whole series must pass, each still running at
    the end of the history, together accounting for at least half the payee's
    occurrences. Anything less falls back to reading the payee as one series,
    so an ordinary bill whose amount drifts (which clusters into singletons)
    keeps exactly the prediction it had before sub-streams existed. A
    sub-stream with too few occurrences is simply not predicted: predicting it
    badly is worse than leaving it out."""
    amount_of, category = core_amounts(conn, items)
    whole = collapse_series(items, amount_of)
    streams = split_substreams(items, amount_of)
    if len(streams) > 1:
        last = max(r["date"] for r in items)
        subs, explained = [], 0
        for rows in streams:
            series = collapse_series(rows, amount_of)
            p = _fit_series(conn, account_id, key, series, today, min_count,
                            _stream_category(rows, category))
            if p is None or not _still_running(p, last):
                continue
            subs.append(p)
            explained += len(series)
        if len(subs) >= 2 and explained * 2 >= len(whole):
            return subs
    p = _fit_series(conn, account_id, key, whole, today, min_count, category)
    return [p] if p is not None else []


def _fit_series(conn, account_id: int, key: str, series, today: str, min_count: int,
                category) -> Optional[Prediction]:
    """One ``collapse_series`` run as a Prediction, or None when it is not
    steady enough to be one."""
    if len(series) < min_count:
        return None
    dates = [d for d, _, _ in series]
    freq = fit_period(dates)
    if freq is None:
        return None
    amounts = [a for _, a, _ in series]
    typical = typical_amount(amounts)
    if typical == 0 or any((a < 0) != (typical < 0) for a in amounts):
        return None
    rows = [r for _, _, r in series]
    payee = rows[-1]["payee"]
    automatic = _automatic_for(conn, payee, rows)
    near = sum(1 for a in amounts if abs(a - typical) <= abs(typical) // 3)
    if len(series) >= 3:
        if near * 5 < len(amounts) * 3:                   # fewer than 60% near the typical
            return None
    elif (max(abs(a - typical) for a in amounts) > abs(typical) // 10
          and not automatic):
        return None                       # two loosely similar rows prove nothing
    nxt, guard = scheduled.advance_date(dates[-1], freq), 0
    while nxt < today and guard < 400:
        nxt, guard = scheduled.advance_date(nxt, freq), guard + 1
    return Prediction(
        account_id=account_id, payee=payee, key=key, amount=typical, frequency=freq,
        last_date=dates[-1], next_date=nxt, count=len(series), automatic=automatic,
        category_id=category, varies=any(a != typical for a in amounts))


def core_amounts(conn, rows) -> tuple:
    """How much of each row is the recurring thing, and which category it is.

    A payee that is usually a plain row of one category -- rent -- sometimes
    arrives as a split: the rent line plus a credit for the refrigerator the
    tenant bought. The deposit total that month says nothing about the rent;
    the rent line does. So when the unsplit rows of a series agree on a
    category, a split row counts for its lines in that category. A payee that
    is ALWAYS split (a paycheck: gross, taxes, deferrals) keeps its total,
    which is what actually lands in the account. Returns ``(amount_of,
    category_id)``: a function from row to cents, and the series' category
    (None when the rows do not agree)."""
    ids = [int(r["id"]) for r in rows]
    split_ids = {int(x[0]) for x in conn.execute(
        f"SELECT DISTINCT transaction_id FROM splits WHERE transaction_id IN "
        f"({','.join('?' for _ in ids)})", ids)} if ids else set()
    plain_cats = [r["category_id"] for r in rows
                  if int(r["id"]) not in split_ids and r["category_id"] is not None]
    category = max(set(plain_cats), key=plain_cats.count) if plain_cats else None
    core: dict = {}
    if category is not None:
        for r in rows:
            if int(r["id"]) in split_ids:
                total = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM splits WHERE transaction_id=? "
                    "AND category_id=?", (int(r["id"]), category)).fetchone()[0]
                if total:
                    core[int(r["id"])] = int(total)
    if category is None and split_ids:
        # every row split: the most common split category names the series
        cats = [x[0] for x in conn.execute(
            f"SELECT category_id FROM splits WHERE transaction_id IN "
            f"({','.join('?' for _ in ids)}) AND category_id IS NOT NULL", ids)]
        category = max(set(cats), key=cats.count) if cats else None

    def amount_of(r) -> int:
        return core.get(int(r["id"]), int(r["amount"]))

    return amount_of, category


# ---------------------------------------------------------------------------
# sub-streams: one payee that is several bills
# ---------------------------------------------------------------------------
# Amount is the cluster key, because a given bill repeats to the cent -- or
# within a few cents of tax and fees -- while two of the same payee's bills for
# different people or different plans differ by dollars. Hence a tight
# tolerance: two percent of the amount, floored at fifty cents so small
# amounts are not split by rounding alone. It is deliberately far tighter than
# the one-third band that lets a SINGLE series' amount drift; that gap is what
# keeps an ordinary varying bill (a power bill of 80, 85, 82) from being read
# as three streams -- it clusters into singletons, none of which qualifies, and
# the payee falls back to being read as one series.
SUBSTREAM_TOLERANCE_PCT = 2
SUBSTREAM_TOLERANCE_FLOOR = 50           # cents

# How far behind the payee's last posting a sub-stream may fall and still count
# as running, in multiples of its own period. A rotation's streams all reach
# the end of the history; the old amount of a bill that simply went up does
# not, and must not be predicted alongside the new one.
SUBSTREAM_STALE_PERIODS = 1.5

_PERIOD_DAYS = {label: days for label, days, _tol in scheduled._INTERVAL_TESTS}


def split_substreams(rows, amount_of=None) -> list:
    """One payee's ``rows`` clustered into the separate bills they are, as a
    list of date-ordered row lists (a single list when the payee looks like one
    series). Amount first; when amount alone does not separate them, memo and
    category as tie-breakers -- the four lines of a family phone plan can cost
    the same and be told apart only by what the memo says. Account is not a
    tie-breaker here: the caller has already grouped by account.

    Splitting is cheap and reversible: the caller (``payee_predictions``) keeps
    a split only when each part independently earns a prediction, so an
    over-eager cluster costs nothing."""
    amount_of = amount_of or (lambda r: int(r["amount"]))
    rows = list(rows)
    clusters = _cluster_by_amount(rows, amount_of)
    if len(clusters) < 2:
        clusters = _cluster_by_tiebreak(rows)
    return [sorted(c, key=lambda r: (r["date"], r["id"])) for c in
            sorted(clusters, key=lambda c: min((r["date"], r["id"]) for r in c))]


def _near_amount(a: int, anchor: int) -> bool:
    """``a`` is the same bill as ``anchor``: same direction of money, and equal
    within the sub-stream tolerance."""
    if (a < 0) != (anchor < 0):
        return False
    tol = max(abs(anchor) * SUBSTREAM_TOLERANCE_PCT // 100, SUBSTREAM_TOLERANCE_FLOOR)
    return abs(a - anchor) <= tol


def _cluster_by_amount(rows, amount_of) -> list:
    """Rows grouped by near-equal amount. Each row is measured against the
    amount that OPENED its cluster, not the previous row, so a long ladder of
    amounts two percent apart does not chain into one cluster spanning
    dollars."""
    buckets: list = []                                   # [(anchor, [rows])]
    for r in sorted(rows, key=lambda r: (amount_of(r), r["date"], r["id"])):
        amount = amount_of(r)
        for anchor, bucket in buckets:
            if _near_amount(amount, anchor):
                bucket.append(r)
                break
        else:
            buckets.append((amount, [r]))
    return [b for _, b in buckets]


def _memo_key(memo) -> str:
    return " ".join((memo or "").lower().split())


def _cluster_by_tiebreak(rows) -> list:
    """Rows grouped by (memo, category): the fallback when every row costs the
    same. A memo that varies month to month scatters the payee into singletons,
    which is harmless -- no singleton earns a prediction, so the payee is read
    as one series again."""
    buckets: dict = {}
    for r in rows:
        buckets.setdefault((_memo_key(r["memo"]), r["category_id"]), []).append(r)
    return list(buckets.values())


def _stream_category(rows, fallback):
    """The category of one sub-stream: what most of its own rows say, falling
    back to the payee-wide answer (rows with no category of their own, or a
    payee that is always split)."""
    cats = [r["category_id"] for r in rows if r["category_id"] is not None]
    return max(set(cats), key=cats.count) if cats else fallback


def _still_running(p: Prediction, payee_last_date: str) -> bool:
    period = _PERIOD_DAYS.get(p.frequency, 30.44)
    behind = (_dt.date.fromisoformat(payee_last_date)
              - _dt.date.fromisoformat(p.last_date)).days
    return behind <= period * SUBSTREAM_STALE_PERIODS


# ---------------------------------------------------------------------------
# reading a series the way a person does
# ---------------------------------------------------------------------------
# Rows closer together than this are one occurrence: a duplicate posting, or a
# payment made in two parts.
COLLAPSE_DAYS = 3


def collapse_series(rows, amount_of=None) -> list:
    """Rows of one payee, in date order, as ``[(date, amount, row)]`` with rows
    within ``COLLAPSE_DAYS`` of the previous one folded in: the same amount
    again (within two percent -- a make-up paycheck after a missed one, or a
    duplicate posting) counts once; a different amount is the rest of a
    payment made in parts and is added. Either way it is one occurrence for
    the interval, and treating it as two broke the interval for every series
    it happened in. ``amount_of`` maps a row to the cents that count (see
    core_amounts); the row total by default."""
    amount_of = amount_of or (lambda r: int(r["amount"]))
    out: list = []
    for r in sorted(rows, key=lambda r: (r["date"], r["id"])):
        amount = amount_of(r)
        if out:
            last_date, last_amount, last_row = out[-1]
            gap = (_dt.date.fromisoformat(r["date"]) - _dt.date.fromisoformat(last_date)).days
            if gap <= COLLAPSE_DAYS:
                if abs(amount - last_amount) <= max(abs(last_amount) // 50, 1):
                    continue                                   # a duplicate
                out[-1] = (last_date, last_amount + amount, last_row)
                continue
        out.append((r["date"], amount, r))
    return out


def fit_period(dates) -> Optional[str]:
    """The recurrence a run of dates fits, or None. The median gap picks the
    candidate interval; then every date is measured against a grid of that
    interval anchored on the LAST date, and the run fits when three quarters
    of them sit within the interval's tolerance of a grid line. Measuring on
    the grid rather than gap by gap forgives what real series do: a payday
    moved for a holiday, a rent paid on the fifth one month and the third the
    next, a period with no posting at all."""
    if len(dates) < 2:
        return None
    ds = [_dt.date.fromisoformat(d) for d in dates]
    gaps = sorted((b - a).days for a, b in zip(ds, ds[1:]))
    median = gaps[len(gaps) // 2]
    label, days, tol = min(scheduled._INTERVAL_TESTS, key=lambda t: abs(median - t[1]))
    if abs(median - days) > tol:
        return None
    last = ds[-1]
    fits = 0
    for d in ds:
        span = (last - d).days
        k = round(span / days)
        if abs(span - k * days) <= tol:
            fits += 1
    return label if fits * 4 >= len(ds) * 3 else None


def typical_amount(amounts) -> int:
    """The amount to expect, in date order of occurrence: the latest when the
    last two agree to the dollar (a rent that went up stays up), else the
    value most occurrences share (the latest such occurrence's exact cents),
    else the median. The latest amount alone was wrong whenever the latest
    happened to be the odd one out -- a partial rent, a bonus month -- and
    the most common alone was wrong once the amount had changed for good."""
    if not amounts:
        return 0
    if len(amounts) >= 2 and amounts[-1] // 100 == amounts[-2] // 100:
        return amounts[-1]
    by_dollar: dict = {}
    for a in amounts:
        by_dollar.setdefault(a // 100, []).append(a)
    dollars, group = max(by_dollar.items(), key=lambda kv: (len(kv[1]), kv[0]))
    if len(group) >= 2:
        return group[-1]
    s = sorted(amounts)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) // 2


def entered_dates(conn, account_ids: Iterable[int], start: str, end: str) -> dict:
    """``{(account_id, payee_key): [dates]}`` of the rows already in the
    registers between ``start`` and ``end`` (widened by the entered window),
    so a predicted occurrence is not shown beside the row that IS it."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return {}
    lo = (_dt.date.fromisoformat(start) - _dt.timedelta(days=ENTERED_WINDOW_DAYS)).isoformat()
    hi = (_dt.date.fromisoformat(end) + _dt.timedelta(days=ENTERED_WINDOW_DAYS)).isoformat()
    out: dict = {}
    for r in conn.execute(
            f"SELECT account_id, payee, date FROM transactions WHERE account_id IN "
            f"({','.join('?' for _ in ids)}) AND date BETWEEN ? AND ? AND payee<>''",
            [*ids, lo, hi]):
        out.setdefault((int(r["account_id"]), payee_key(r["payee"])), []).append(r["date"])
    return out


def is_entered(entered: dict, account_id: int, key: str, date: str) -> bool:
    d = _dt.date.fromisoformat(date)
    for other in entered.get((int(account_id), key), ()):
        if abs((_dt.date.fromisoformat(other) - d).days) <= ENTERED_WINDOW_DAYS:
            return True
    return False


# ---------------------------------------------------------------------------
# dismissals
# ---------------------------------------------------------------------------
def dismiss(conn, account_id: int, payee: str) -> None:
    """Stop predicting ``payee`` on this account (the user judged the estimate
    wrong). Restorable."""
    conn.execute(
        "INSERT INTO prediction_dismissals(account_id, payee_key, payee) VALUES (?,?,?) "
        "ON CONFLICT(account_id, payee_key) DO UPDATE SET payee=excluded.payee, "
        "dismissed_at=datetime('now')", (int(account_id), payee_key(payee), payee))
    conn.commit()


def restore(conn, account_id: int, payee_key_: str) -> None:
    conn.execute("DELETE FROM prediction_dismissals WHERE account_id=? AND payee_key=?",
                 (int(account_id), payee_key_))
    conn.commit()


def dismissed(conn) -> list:
    """Every dismissal as a dict with the account's name, newest first."""
    out = []
    for r in conn.execute(
            "SELECT d.id, d.account_id, d.payee_key, d.payee, d.dismissed_at, a.name "
            "FROM prediction_dismissals d JOIN accounts a ON a.id = d.account_id "
            "ORDER BY d.dismissed_at DESC, d.id DESC"):
        out.append({"id": int(r["id"]), "account_id": int(r["account_id"]),
                    "account_name": r["name"], "payee_key": r["payee_key"],
                    "payee": r["payee"], "dismissed_at": r["dismissed_at"]})
    return out
