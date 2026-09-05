"""Shared insert path for all importers.

A parser hands :func:`import_records` a list of :class:`NormalizedTxn`. This
module owns everything that touches the database:

* resolve each record's account (get-or-create by name) and category
  (get-or-create the hierarchical Parent:Child path),
* DEDUP against what is already stored -- exact by ``fitid`` within the account,
  otherwise a fuzzy amount/date/payee score recorded in ``transaction_matches``,
* COLLAPSE Quicken transfer mirrors: the two ``[Account]`` sides of one transfer
  (whether both are in this file or the second is a re-import) become a single
  :func:`mammon.ledger.create_transfer`, never two independent rows,
* record the run in the ``imports`` table with added/duplicate/error counts.

The ledger layer (:mod:`mammon.ledger`) remains the only writer of transaction
rows, so transfer invariants (mirror signs, pair links, net-worth zero) are
enforced in exactly one place.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Optional

from mammon import ledger
from mammon.importers.record import (
    ImportResult,
    NormalizedTxn,
    iso_shift,
    normalize_payee,
)

# Fuzzy-dedup window and acceptance threshold.
_DATE_WINDOW_DAYS = 3
_DUP_THRESHOLD = 0.80


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------
def import_records(
    conn: sqlite3.Connection,
    records: list[NormalizedTxn],
    provider: Optional[str] = None,
    source_format: Optional[str] = None,
    filename: Optional[str] = None,
    file_hash: Optional[str] = None,
    default_account: Optional[str] = None,
    default_account_type: str = "checking",
    securities=None,
    prices=None,
    positions=None,
    categories=None,
    tags=None,
) -> ImportResult:
    """Ingest ``records`` into the ledger, dedup, and record the run.

    ``securities`` ((name, symbol, type) tuples) and ``prices`` ((symbol, iso,
    close) tuples) come from a QIF's security master / price history: prices are
    recorded (keyed by the security NAME, which is what investment_transactions
    store), and holdings are rebuilt for every investment account touched, so the
    accounts can be valued at market."""
    import_id = conn.execute(
        "INSERT INTO imports(provider, source_format, filename, file_hash, status) "
        "VALUES (?,?,?,?, 'pending')",
        (provider, source_format, filename, file_hash),
    ).lastrowid
    conn.commit()

    result = ImportResult(import_id=import_id)
    acct_cache: dict[str, int] = {}
    cat_cache: dict[str, int] = {}

    plain: list[NormalizedTxn] = []
    transfers: list[NormalizedTxn] = []
    investments: list[NormalizedTxn] = []
    for r in records:
        if not (r.external_account or default_account):
            result.errors += 1
            continue
        if not r.external_account:
            r.external_account = default_account
        if r.account_type in (None, "", "checking") and default_account_type:
            # "checking" is the NormalizedTxn default (an unset sentinel), so a
            # caller's default_account_type (e.g. "credit" for a card CSV) wins.
            r.account_type = default_account_type
        if r.is_investment:
            investments.append(r)
        elif r.is_transfer:
            transfers.append(r)
        else:
            plain.append(r)

    # Count-aware multiset dedup (import-vs-register only): each already-stored
    # register row may be the duplicate-target of at most ONE incoming row, so N
    # identical rows in this batch dedup against the R identical rows ALREADY
    # stored and insert only the surplus max(0, N - R). `claimed`/`claimed_inv`
    # hold the ids already consumed as a dup-target, so the next identical row in
    # the same import cannot reuse them and therefore lands. Within-batch siblings
    # are never collapsed (a row inserted by THIS import carries this import_id and
    # is excluded from every dedup query below).
    claimed: set[int] = set()
    claimed_inv: set[int] = set()
    _apply_categories(conn, categories)
    _apply_tag_master(conn, tags)
    # Which accounts this FILE carries a register for, by date: the test for
    # whether the counter-side of a transfer will arrive on its own.
    covered = _covered_accounts(conn, plain + transfers + investments, acct_cache)
    for r in plain:
        _import_plain(conn, r, import_id, acct_cache, cat_cache, result, claimed, covered)
    _import_transfers(conn, transfers, import_id, acct_cache, result, claimed, covered)
    inv_account_ids: set[int] = set()
    for r in investments:
        aid = _import_investment(conn, r, import_id, acct_cache, result, claimed_inv)
        if aid is not None:
            inv_account_ids.add(aid)

    _apply_securities_and_prices(conn, securities, prices, inv_account_ids)
    _apply_positions(conn, positions, inv_account_ids, result)

    # Build the year-end cash balance snapshots for every account this import
    # touched, so the live read path (ledger.account_balance) serves balances
    # from the snapshot cache instead of summing from inception.
    for aid in ledger.account_max_dates_for_import(conn, import_id):
        ledger.rebuild_checkpoints(conn, aid)

    if (source_format or "").lower() == "qif":
        # This is the one-time full-history Quicken migration. Watermark every
        # account it touched with its newest imported date so the FIRST live
        # OFX/QFX pull skips the fitid-less overlap instead of double-importing it
        # (closes gap G4). Monotonic: never moves an existing watermark earlier.
        for aid, d in ledger.account_max_dates_for_import(conn, import_id).items():
            ledger.set_account_cutover_date(conn, aid, d)

    conn.execute(
        "UPDATE imports SET status='done', added_count=?, duplicate_count=?, error_count=? "
        "WHERE id=?",
        (result.added, result.duplicates, result.errors, import_id),
    )
    conn.commit()
    return result


def _apply_categories(conn, categories) -> None:
    """A QIF category list (``(path, income|expense|None)`` tuples): create
    what is missing and give a category its kind when it has none yet. A kind
    the user or an earlier import already set is left alone."""
    for path, typ in categories or []:
        if not path:
            continue
        cid = ledger.resolve_category(conn, path)
        if cid is None or not typ:
            continue
        row = conn.execute("SELECT type FROM categories WHERE id=?", (cid,)).fetchone()
        if row is not None and not row["type"]:
            conn.execute("UPDATE categories SET type=? WHERE id=?", (typ, cid))
    conn.commit()


def _apply_tag_master(conn, tags) -> None:
    """A QIF tag list (``(name, description)`` tuples): create what is missing
    and give a tag its description when it has none yet.

    Carried for the same reason as the category list -- a tag the user defined
    but has not used on any transaction still exists, and the description is the
    only place its meaning is recorded. A description already set is left alone,
    so re-importing an older export cannot overwrite a newer one."""
    for name, description in tags or []:
        tid = ledger.tag_id(conn, name)
        if tid is None or not description:
            continue
        row = conn.execute("SELECT description FROM tags WHERE id=?", (tid,)).fetchone()
        if row is not None and not row["description"]:
            conn.execute("UPDATE tags SET description=? WHERE id=?", (description, tid))
    conn.commit()


def _apply_securities_and_prices(conn, securities, prices, inv_account_ids) -> None:
    """After investment rows land: record the parsed price history and rebuild
    holdings for every investment account touched. Prices are keyed by the
    security NAME (Quicken's ``Y`` field, which investment_transactions and hence
    holdings use) -- the security master maps the price section's ticker SYMBOL
    back to that name."""
    from mammon import investments
    from mammon import securities as securities_mod

    # KEEP the security master, do not merely consult it. Quicken states the
    # ticker (``S``) and the type (``T``) beside the name, and this function used
    # to read ``S`` only to translate the price section back to a name and then
    # throw it away -- nothing wrote the `securities` table at all. The file was
    # left holding a concatenated name and no ticker, so when a later source
    # supplied the bare ticker it became a SECOND security (SRD 5.8e-2).
    # Recording it is not a rename: rows keep their current identity, and
    # `securities.suggest` reads this back so a stated ticker never has to be
    # guessed at.
    if securities:
        securities_mod.record_master(conn, securities)

    # Capture transaction-carried prices (the QIF ``I`` field on Buy/Sell/ReinvDiv)
    # into price_history, recorded with source 'qif-txn' and DO-NOTHING precedence
    # so an explicit !Type:Prices quote (recorded just below, DO-UPDATE) always
    # wins over a transaction-carried price for the same (symbol, date).
    for aid in inv_account_ids:
        investments.learn_prices_from_transactions(conn, aid)

    if prices:
        sym_to_name = {}
        for name, symbol, _type in (securities or []):
            symbol = (symbol or "").strip()
            name = (name or "").strip()
            if symbol and name:
                sym_to_name.setdefault(symbol, name)
        rows = [
            (sym_to_name.get(sym, sym), date, close, "qif")
            for (sym, date, close) in prices
        ]
        investments.record_prices(conn, rows)

    for aid in inv_account_ids:
        investments.rebuild_holdings(conn, aid)


def _apply_positions(conn, positions, inv_account_ids, result) -> None:
    """Reconcile a broker's ``<INVPOS>`` holdings snapshot against the holdings
    just computed from transactions, THEN land the reported unit prices. Order
    matters: reconcile first, so a conflicting on-file price is surfaced before
    the position price is (non-overwritingly) recorded, and so the computed
    holdings -- never the broker's numbers -- stay authoritative."""
    if not positions:
        return
    from mammon import investments

    for aid in inv_account_ids:
        result.position_discrepancies.extend(
            investments.reconcile_positions(conn, aid, positions)
        )
    investments.record_position_prices(conn, positions)


# ---------------------------------------------------------------------------
# plain cash
# ---------------------------------------------------------------------------
def _import_plain(conn, r, import_id, acct_cache, cat_cache, result, claimed,
                  covered=None) -> None:
    account_id = _resolve_account(
        conn, r.external_account, r.account_type, acct_cache, authoritative=True
    )
    if _before_cutover(conn, account_id, r):
        result.duplicates += 1
        return
    existing_id, score, method = _find_dup_cash(conn, account_id, r, import_id, claimed)
    if existing_id is not None:
        claimed.add(existing_id)
        result.duplicates += 1
        if method == "fuzzy":
            conn.execute(
                "INSERT INTO transaction_matches(imported_txn_id, existing_txn_id, score, approved) "
                "VALUES (NULL, ?, ?, 1)",
                (existing_id, score),
            )
            conn.commit()
        return

    # Scheduled-payment match (Task 55): a lumped loan payment may have been
    # pre-entered as a pending split a few days before its due date. Post the
    # import INTO that placeholder -- fill fitid/import_id, mark it cleared, and
    # re-derive the split for the actual amount -- instead of creating a
    # duplicate row. Source-split records are left to the normal insert path.
    # Three placeholder shapes take part, in this order:
    #   * a payment pre-entered on the LOAN register (a loan with no known
    #     funding account) -- merged when the import lands on the loan;
    #   * a payment pre-entered on the FUNDING account (the user's real model:
    #     interest + escrow + a [Loan] principal leg on checking) -- merged when
    #     the funder's download brings the debit;
    #   * a plain pre-entry from a definition (a bill, a paycheck, a transfer
    #     leg) -- merged on amount within a few days, keeping the definition's
    #     own payee and category.
    # Without the last two, every pre-entry became a duplicate the moment the
    # real row downloaded, which made pre-entering worse than not.
    from mammon import loans, loans_schedule, scheduled as _scheduled
    if not r.splits:
        hit = loans_schedule.find_matching_funding_pending(
            conn, account_id, r.date, r.amount_cents)
        if hit is None and r.amount_cents < 0:
            # No pre-entry at this amount. When the bank's descriptor names the
            # pre-entry's payee and the amount is in the same range, the
            # payment CHANGED (escrow / rate) after it was scheduled: merge into
            # it and record the change for the user to confirm, rather than a
            # duplicate beside a placeholder that then stands forever. The
            # payee is the signal -- a different debit of a similar size in
            # the window must never be taken for the mortgage.
            hit = _funding_pending_by_payee(conn, account_id, r)
            if hit is not None:
                pend = ledger.get_transaction(conn, hit[0])
                result.payment_changes.append(loans_schedule.PaymentChange(
                    hit[0], pend["date"], -int(pend["amount"]), -r.amount_cents))
        if hit is not None:
            pending_id, loan_id = hit
            loans_schedule.merge_import_into_funding_pending(
                conn, pending_id, loan_id, date=r.date, amount_cents=r.amount_cents,
                fitid=r.fitid or None, import_id=import_id)
            result.matched += 1
            return
        pending_id = _scheduled.find_matching_placeholder(
            conn, account_id, r.date, r.amount_cents)
        if pending_id is None:
            pending_id = _placeholder_by_payee(conn, account_id, r)
        if pending_id is not None:
            _scheduled.merge_import_into_placeholder(
                conn, pending_id, date=r.date, amount_cents=r.amount_cents,
                fitid=r.fitid or None, import_id=import_id)
            result.matched += 1
            return
    if r.amount_cents > 0 and not r.splits \
            and loans.get_loan_params(conn, account_id) is not None:
        # The lender's own download shows the principal applied: that is the
        # pending MIRROR of the funding-side pre-entry -- confirm it, never
        # file it as a second payment or re-split it.
        mirror_id = loans_schedule.find_matching_pending_mirror(
            conn, account_id, r.date, r.amount_cents)
        if mirror_id is not None:
            loans_schedule.confirm_pending_mirror(
                conn, mirror_id, fitid=r.fitid or None, import_id=import_id)
            result.matched += 1
            return
        pending_id = loans_schedule.find_matching_pending(
            conn, account_id, r.date, r.amount_cents)
        if pending_id is not None:
            loans_schedule.merge_import_into_pending(
                conn, pending_id, date=r.date, amount_cents=r.amount_cents,
                fitid=r.fitid or None, import_id=import_id,
                payee=r.payee or None)
            result.matched += 1
            return
        # Payment CHANGE (Task 56): no pre-entry matched the amount exactly, but
        # one sits within the date window with a DIFFERENT amount -- the payment
        # changed (rate reset / escrow adjustment) after we scheduled it. Merge
        # the import into that pre-entry so we don't duplicate (its split
        # temporarily absorbs the delta into principal) and FLAG the mismatch for
        # the user-confirmed downstream auto-fix; nothing is silently re-rated.
        change = loans_schedule.detect_payment_change(
            conn, account_id, r.date, r.amount_cents)
        if change is not None:
            loans_schedule.merge_import_into_pending(
                conn, change.pending_id, date=r.date, amount_cents=r.amount_cents,
                fitid=r.fitid or None, import_id=import_id,
                payee=r.payee or None)
            result.matched += 1
            result.payment_changes.append(change)
            return

    # Loan-payment split (Task 52): a lumped payment imported against a loan
    # account (one carrying loan_params) is expanded into its principal /
    # interest / escrow category legs, so interest and escrow are preserved
    # instead of collapsing into one uncategorized amount. Source-provided
    # splits and non-loan accounts are left untouched.
    if not r.splits:
        loan_legs = _loan_payment_splits(conn, account_id, r)
        if loan_legs is not None:
            r.splits = loan_legs

    category_id = _resolve_category(conn, r.category, cat_cache)
    _upsert_payee(conn, r.payee)
    txn_id = ledger.add_transaction(
        conn,
        account_id,
        r.date,
        r.amount_cents,
        payee=r.payee or None,
        memo=r.memo or None,
        category_id=category_id,
        num=r.check_number or None,
        fitid=r.fitid or None,
        cleared=r.cleared,
        reconciled=r.reconciled,
        import_id=import_id,
    )
    if r.tags:
        ledger.set_tags(conn, txn_id, r.tags)
    for cat, amt, memo, leg_tag in r.splits:
        _insert_split(conn, txn_id, cat, amt, memo, acct_cache, cat_cache,
                      covered=covered, date=r.date, tag=leg_tag)
    if r.splits:
        conn.commit()
    result.added += 1


# ---------------------------------------------------------------------------
# loan-payment category split (Task 52)
# ---------------------------------------------------------------------------
# Default category paths for the two legs a loan's own parameters do not name
# (each escrow/PMI/HOA extra keeps its OWN configured category). "Interest Exp"
# is Quicken's usual mortgage-interest category; the principal leg is the loan
# paydown itself.
LOAN_INTEREST_CATEGORY = "Interest Exp"
LOAN_PRINCIPAL_CATEGORY = "Principal"


def _loan_payment_splits(conn, account_id, r):
    """The category split legs for a lumped payment imported against a loan
    account, or ``None`` to leave the record untouched.

    When ``account_id`` carries loan parameters (Task 50) and ``r`` is a plain,
    principal-reducing payment with no split of its own, decompose it with
    :func:`mammon.loans.payment_split` into ``[(category, cents, memo, tag), ...]`` --
    the interest leg, one leg per categorized extra (escrow/PMI/HOA...), and the
    principal leg -- which reconstitute the payment TO THE CENT (their signed
    cents sum to ``r.amount_cents``). Returns ``None`` for a non-loan account, a
    record that already carries splits, a draw/charge (an amount that does not
    reduce the liability), or a payment too small to cover the period's interest
    plus extras (principal would be non-positive) -- none of which should be
    force-split."""
    from mammon import loans

    if r.splits or r.amount_cents <= 0:
        return None
    lp = loans.get_loan_params(conn, account_id)
    if lp is None:
        return None
    split = loans.payment_split(conn, account_id, r.date, r.amount_cents)
    if split.principal <= 0:
        return None
    legs: list[tuple] = []
    if split.interest:
        legs.append((lp.interest_category or LOAN_INTEREST_CATEGORY,
                     split.interest, "Interest", ""))
    for ex in split.extras:
        if ex.amount:
            legs.append((ex.category, ex.amount, ex.label or ex.category, ""))
    legs.append((LOAN_PRINCIPAL_CATEGORY, split.principal, "Principal", ""))
    if len(legs) < 2:                      # a single leg is just a plain category
        return None
    if sum(amt for _cat, amt, _memo, _tag in legs) != r.amount_cents:
        return None                        # safety: never post a mis-totalled split
    return legs


# ---------------------------------------------------------------------------
# transfers (collapse mirror pairs; dedup against existing)
# ---------------------------------------------------------------------------
def _covered_accounts(conn, records, acct_cache) -> set:
    """``{(account_id, date)}`` for every record in the batch, keyed by the
    account the record BELONGS to. A transfer leg whose counter-account is not
    in this set has no other side coming: nothing later in the file will
    supply it (see :func:`_import_transfer_leg`)."""
    out: set = set()
    for r in records:
        name = (r.external_account or "").strip()
        if not name or not r.date:
            continue
        out.add((_resolve_account(conn, name, r.account_type or "checking", acct_cache), r.date))
    return out


def _counterparty_absent(conn, covered, account_name, date, acct_cache) -> bool:
    """True when the file carries no register for ``account_name`` on ``date``,
    so this leg is the only record of the transfer and its mirror must be
    created here."""
    if covered is None:
        return False
    name = (account_name or "").strip()
    if not name:
        return False
    return (_resolve_account(conn, name, "checking", acct_cache), date) not in covered


def _import_transfers(conn, transfers, import_id, acct_cache, result, claimed,
                      covered=None) -> None:
    groups: dict[tuple, list[NormalizedTxn]] = defaultdict(list)
    for r in transfers:
        a = _resolve_account(
            conn, r.external_account, r.account_type, acct_cache, authoritative=True
        )
        b = _resolve_account(conn, r.transfer_account, "checking", acct_cache)
        if a == b:
            # Quicken writes an account's opening balance as a transfer to
            # itself payee'd "Opening Balance" (so does mammon.export). That is
            # the account's opening balance, not a transaction: adopt it when
            # the account holds nothing from before THIS import (no opening
            # balance, no rows from earlier imports or by hand -- transfers are
            # collapsed after the file's plain rows land, so the file's own
            # rows do not count), skip it when the account already says the
            # same, and otherwise fall through to the plain row it always was.
            # A ledger whose accounts were populated under the older reading
            # (the opening balance as a +row) therefore sees a re-import
            # exactly as before: the row dedups against itself.
            if _squash(r.payee) == "openingbalance":
                acct = ledger.get_account(conn, a)
                current = int(acct["opening_balance"] or 0) if acct is not None else 0
                fresh = conn.execute(
                    "SELECT 1 FROM transactions WHERE account_id=? "
                    "AND (import_id IS NULL OR import_id<>?) LIMIT 1", (a, import_id)
                ).fetchone() is None
                if current == 0 and fresh and r.amount_cents:
                    ledger.set_opening_balance(conn, a, r.amount_cents, r.date)
                    continue
                if current == r.amount_cents:
                    result.duplicates += 1
                    continue
            # any other self-transfer -> a plain row instead of a mirror
            _import_plain(conn, r, import_id, acct_cache, {}, result, claimed)
            continue
        key = (frozenset((a, b)), r.date, abs(r.amount_cents))
        groups[key].append(r)

    for recs in groups.values():
        while recs:
            r = recs.pop()
            mirror = _pop_mirror(recs, r)

            if mirror is None:
                # ASYMMETRIC or single-sided leg: no equal-and-opposite mirror in
                # the batch. a real Quicken data has multi-way and split
                # transfers -- a property purchase split across a mortgage and a
                # cash down payment; a mortgage payment whose checking line is the
                # full payment while the loan line is only the principal -- so the
                # two register lines carry DIFFERENT amounts and cannot collapse
                # into one equal-and-opposite pair. Record ONLY this account's own
                # leg (its own signed amount, linked to the counter-account) so its
                # balance matches its own Quicken register; NEVER fabricate the
                # counterparty side (doing so double-counted it: e.g. a $120k condo
                # purchase debited checking $120k on top of its real $22,000.00
                # down payment).
                if r.amount_cents == 0:
                    _import_plain(conn, r, import_id, acct_cache, {}, result, claimed, covered)
                elif _counterparty_absent(conn, covered, r.transfer_account, r.date, acct_cache):
                    # The file holds no register for the counter-account on this
                    # date, so no other side is coming and this leg is the whole
                    # transfer: create the linked pair, as Quicken does with a
                    # one-sided [Account] line. (mammon.export writes each
                    # transfer once for exactly this reason -- given both sides,
                    # Quicken matched them and kept the bare transfer over the
                    # split it came from, losing the paycheck's other legs.)
                    _import_transfer_pair(conn, r, import_id, acct_cache, result, claimed)
                else:
                    _import_transfer_leg(conn, r, import_id, acct_cache, result, claimed)
                continue

            # TRUE symmetric transfer: both equal-and-opposite sides are present,
            # so collapse into a single linked mirror pair (Quicken transfer).
            if r.amount_cents < 0:
                from_name, to_name = r.external_account, r.transfer_account
                from_rec, to_rec = r, mirror
            else:
                from_name, to_name = mirror.external_account, mirror.transfer_account
                from_rec, to_rec = mirror, r
            from_id = _resolve_account(conn, from_name, "checking", acct_cache)
            to_id = _resolve_account(conn, to_name, "checking", acct_cache)
            amount = abs(r.amount_cents)

            if amount == 0:
                # A zero-amount "transfer" moves no money -- e.g. a refinance entry
                # whose top-level nets to zero but carries split lines. create_transfer
                # rejects a non-positive amount, so import each side as a PLAIN row
                # (preserving its splits) instead of crashing the whole import.
                _import_plain(conn, r, import_id, acct_cache, {}, result, claimed)
                _import_plain(conn, mirror, import_id, acct_cache, {}, result, claimed)
                continue

            existing_leg = _transfer_exists(
                conn, from_id, to_id, r.date, amount, import_id, claimed)
            if existing_leg is not None:
                claimed.add(existing_leg)
                result.duplicates += 2
                continue
            fid, tid = ledger.create_transfer(
                conn,
                from_id,
                to_id,
                r.date,
                amount,
                memo=r.memo or mirror.memo or None,
                num=r.check_number or None,
                # Quicken keeps a normal payee on both legs of a transfer; the
                # QIF `P` line is captured per leg, so preserve whichever side
                # carried one (both legs then share the same payee text).
                payee=(r.payee or mirror.payee or "").strip() or None,
                # Carry the Quicken `C` (cleared/reconciled) flag through so an
                # imported transfer's Clr column renders instead of showing a
                # BLANK. create_transfer stamps BOTH legs with the from-leg's
                # status; the to-leg is corrected below. Without this the
                # collapse path dropped the status entirely, so every imported
                # transfer arrived cleared=0/reconciled=0 -- a blank Clr amid an
                # otherwise fully-reconciled ledger.
                cleared=from_rec.cleared,
                reconciled=from_rec.reconciled,
            )
            # Each leg reconciles INDEPENDENTLY against its own account's bank
            # statement (Quicken keeps the two Clr flags separate -- one side may
            # be reconciled while the other is not), so give the to-leg its OWN
            # QIF status rather than mirroring the from-leg onto it.
            if (to_rec.cleared, to_rec.reconciled) != (
                from_rec.cleared,
                from_rec.reconciled,
            ):
                conn.execute(
                    "UPDATE transactions SET cleared=?, reconciled=? WHERE id=?",
                    (to_rec.cleared, to_rec.reconciled, tid),
                )
            conn.execute(
                "UPDATE transactions SET import_id=? WHERE id IN (?,?)",
                (import_id, fid, tid),
            )
            if r.fitid:
                conn.execute("UPDATE transactions SET fitid=? WHERE id=?", (r.fitid, fid))
            conn.commit()
            result.added += 1
            result.transfers += 1


def _import_transfer_pair(conn, r, import_id, acct_cache, result, claimed) -> None:
    """Insert a one-sided ``[Account]`` line as a LINKED PAIR -- both register
    rows, cross-linked -- because the file carries no register for the
    counter-account on that date, so nothing else will supply the other side.
    This is what Quicken does with such a line, and what mammon.export relies
    on: it writes each transfer exactly once (see _transfer_skips). When the
    counter-account IS in the file, the caller uses _import_transfer_leg
    instead and fabricates nothing, which is what keeps an asymmetric
    multi-way transfer -- a house purchase split across a mortgage and a cash
    down payment -- from double-counting."""
    account_id = _resolve_account(
        conn, r.external_account, r.account_type, acct_cache, authoritative=True
    )
    counter_id = _resolve_account(conn, r.transfer_account, "checking", acct_cache)
    if r.amount_cents < 0:
        from_id, to_id, amount = account_id, counter_id, -r.amount_cents
    else:
        from_id, to_id, amount = counter_id, account_id, r.amount_cents
    existing = _transfer_exists(conn, from_id, to_id, r.date, amount, import_id, claimed)
    if existing is not None:
        claimed.add(existing)
        result.duplicates += 1
        return
    _upsert_payee(conn, r.payee)
    fid, tid = ledger.create_transfer(
        conn, from_id, to_id, r.date, amount,
        payee=r.payee or None, memo=r.memo or None, num=r.check_number or None,
    )
    own = fid if from_id == account_id else tid
    fields = {"import_id": import_id}
    if r.cleared or r.reconciled:
        fields.update(cleared=r.cleared, reconciled=r.reconciled)
    if r.fitid:
        fields["fitid"] = r.fitid
    ledger.update_transaction(conn, own, **fields)
    conn.commit()
    result.added += 1
    result.transfers += 1


def _import_transfer_leg(conn, r, import_id, acct_cache, result, claimed) -> None:
    """Insert a ONE-SIDED transfer leg: a transaction in its own account carrying
    its OWN signed amount and linked to the counter-account via
    transfer_account_id, but with NO mirror (transfer_pair_id stays NULL). Used
    for asymmetric / multi-way transfers whose two Quicken register lines carry
    different amounts and so cannot collapse into a single equal-and-opposite
    pair. transfer_pair_id NULL is a safe, supported state -- update_transaction
    and delete_transaction only sync / cascade to a mirror when it is non-NULL."""
    account_id = _resolve_account(
        conn, r.external_account, r.account_type, acct_cache, authoritative=True
    )
    counter_id = _resolve_account(conn, r.transfer_account, "checking", acct_cache)
    # Idempotent re-import: skip an identical leg already booked by another run.
    # Count-aware: consume each existing leg at most once, so two legitimately
    # identical legs in one import both land against R existing (insert surplus).
    dups = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND transfer_account_id=? "
        "AND date=? AND amount=? AND (import_id IS NULL OR import_id != ?)",
        (account_id, counter_id, r.date, r.amount_cents, import_id),
    ).fetchall()
    dup_id = next((row["id"] for row in dups if row["id"] not in claimed), None)
    if dup_id is not None:
        claimed.add(dup_id)
        result.duplicates += 1
        return
    _upsert_payee(conn, r.payee)
    category_id = _resolve_category(conn, r.category, {}) if r.category else None
    txn_id = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, memo, num, "
        "cleared, reconciled, category_id, transfer_account_id, "
        "fitid, import_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, r.date, r.amount_cents, r.payee or None, r.memo or None,
         r.check_number or None, r.cleared, r.reconciled,
         category_id, counter_id, r.fitid or None, import_id),
    ).lastrowid
    if r.tags:
        ledger.set_tags(conn, txn_id, r.tags)
    for cat, amt, memo, leg_tag in r.splits:
        _insert_split(conn, txn_id, cat, amt, memo, acct_cache, {}, tag=leg_tag)
    conn.commit()
    result.added += 1
    result.transfers += 1


def _pop_mirror(recs, r) -> Optional[NormalizedTxn]:
    """Remove and return the opposite side of ``r`` from ``recs`` if present:
    the mirror lives in the counter-account with the opposite sign."""
    for i, other in enumerate(recs):
        if (
            other.external_account == r.transfer_account
            and other.transfer_account == r.external_account
            and other.amount_cents == -r.amount_cents
        ):
            return recs.pop(i)
    return None


def _transfer_exists(conn, from_id, to_id, date, amount, import_id, claimed) -> Optional[int]:
    """Return the id of an existing from-leg this transfer duplicates, else None.

    Count-aware: an already-matched leg id in ``claimed`` is skipped, so two
    identical transfers in one import dedup against R existing pairs and insert the
    surplus rather than all collapsing onto a single existing leg."""
    rows = conn.execute(
        "SELECT id FROM transactions "
        "WHERE account_id=? AND transfer_account_id=? AND date=? AND amount=? "
        "AND (import_id IS NULL OR import_id != ?)",
        (from_id, to_id, date, -amount, import_id),
    ).fetchall()
    return next((row["id"] for row in rows if row["id"] not in claimed), None)


# ---------------------------------------------------------------------------
# investments
# ---------------------------------------------------------------------------
def _import_investment(conn, r, import_id, acct_cache, result, claimed) -> Optional[int]:
    """Insert one investment transaction; returns its account id so the caller can
    rebuild that account's holdings after the batch (or on a duplicate, so a
    re-import still refreshes holdings)."""
    account_id = _resolve_account(
        conn, r.external_account, r.account_type or "investment", acct_cache,
        authoritative=True,
    )
    if _before_cutover(conn, account_id, r):
        result.duplicates += 1
        return account_id
    dup_id = _find_dup_investment(conn, account_id, r, import_id, claimed)
    if dup_id is not None:
        claimed.add(dup_id)
        result.duplicates += 1
        return account_id
    transfer_account_id = None
    if r.transfer_account:
        transfer_account_id = _resolve_account(conn, r.transfer_account, "checking", acct_cache)
    conn.execute(
        "INSERT INTO investment_transactions"
        "(account_id, date, action, symbol, quantity, price, amount, commission, memo, "
        " transfer_account_id, import_id, fitid, split_num, split_den)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            account_id,
            r.date,
            r.action,
            r.symbol or None,
            r.quantity or None,
            r.price or None,
            r.amount_cents,
            r.commission_cents or None,
            r.memo or None,
            transfer_account_id,
            import_id,
            r.fitid or None,
            r.split_num or None,
            r.split_den or None,
        ),
    )
    conn.commit()
    result.added += 1
    result.investments += 1
    return account_id


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------
def _before_cutover(conn, account_id, r) -> bool:
    """True if this incoming row is already covered by migrated history.

    The one-time full Quicken (QIF) migration leaves rows with fitid=NULL, so the
    first live OFX/QFX pull for the account cannot dedup the overlap by fitid --
    and fuzzy/tuple matching is not guaranteed to catch every row. The account's
    ``cutover_date`` is the newest migrated date; any incoming import row dated
    on/before it is already represented and is skipped (closes gap G4 -- the
    migration seam). Rows after the watermark fall through to the normal fitid /
    fuzzy dedup below."""
    cutover = ledger.account_cutover_date(conn, account_id)
    return bool(cutover) and bool(r.date) and r.date <= cutover


def _squash(payee) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_payee(payee or "").lower())


def _same_payee(want: str, have: str) -> bool:
    """Squashed payee names that are equal or one contains the other -- the
    definition's clean name ("Power Co") inside the bank's descriptor
    ("POWER CO ONLINE PMT"). Short names are never trusted."""
    return (len(want) >= 4 and len(have) >= 4
            and (want == have or want in have or have in want))


def _placeholder_by_payee(conn, account_id, r) -> Optional[int]:
    """A pending placeholder on the account within the window whose PAYEE is
    the incoming row's -- same sign, names per _same_payee -- the nearest by
    date, or None. The exact-amount match comes first; this is the fallback
    for a bill whose amount moves month to month, which otherwise never met
    its placeholder."""
    from mammon import scheduled as _scheduled
    want = _squash(r.payee)
    for row in _scheduled.pending_placeholders(conn, account_id, r.date):
        if (int(row["amount"]) < 0) != (r.amount_cents < 0):
            continue
        if _same_payee(want, _squash(row["payee"])):
            return int(row["id"])
    return None


def _funding_pending_by_payee(conn, account_id, r):
    """The pending funding-side loan pre-entry in the window whose payee the
    incoming descriptor names and whose amount is within half of it, as
    ``(pending_id, loan_account_id)``, or None -- the changed-payment case."""
    from mammon import loans_schedule
    want = _squash(r.payee)
    for row in loans_schedule.pending_funding_pre_entries(conn, account_id, r.date):
        expected = -int(row["amount"])
        if not _same_payee(want, _squash(row["payee"])):
            continue
        if abs(-r.amount_cents - expected) > expected // 2:
            continue
        return int(row["id"]), int(row["loan_id"])
    return None


def _find_dup_cash(conn, account_id, r, import_id, claimed):
    """Return (existing_txn_id, score, method) or (None, 0.0, None).

    Rows from the CURRENT import are excluded so two legitimately-identical rows
    in one source file both land; a later re-import (new import_id) dedups them.

    Count-aware: any register id already in ``claimed`` (matched by an earlier row
    of THIS import) is skipped, so N identical incoming rows consume N DISTINCT
    existing rows -- when the register holds fewer than N, the surplus falls
    through to a fresh insert instead of all collapsing onto one existing row.
    """
    if r.fitid:
        rows = conn.execute(
            "SELECT id FROM transactions WHERE account_id=? AND fitid=? "
            "AND (import_id IS NULL OR import_id != ?)",
            (account_id, r.fitid, import_id),
        ).fetchall()
        for row in rows:
            if row["id"] not in claimed:
                return row["id"], 1.0, "fitid"
    lo, hi = iso_shift(r.date, -_DATE_WINDOW_DAYS), iso_shift(r.date, _DATE_WINDOW_DAYS)
    # NOTE: candidates are NOT restricted to transfer_account_id IS NULL. The same
    # logical transaction can be classified as a PLAIN row in one source file and
    # as a TRANSFER leg (bracketed [Account] category) in another, so a re-import
    # that flips the classification must still dedup against the existing row --
    # otherwise it double-counts (watchdog: Anytown CU Ck was off ~$70k because a
    # mortgage payment first imported as a transfer to the loan was re-imported as
    # a plain row and inserted twice). Match on date+amount+payee regardless of
    # transfer_account_id. `scheduled=0` still stands: a pending scheduled row is a
    # placeholder handled by the loans_schedule merge path, not a dedup target.
    rows = conn.execute(
        "SELECT id, date, payee FROM transactions "
        "WHERE account_id=? AND amount=? AND date BETWEEN ? AND ? "
        "AND scheduled=0 "
        "AND (import_id IS NULL OR import_id != ?)",
        (account_id, r.amount_cents, lo, hi, import_id),
    ).fetchall()
    best_id, best_score = None, 0.0
    want = normalize_payee(r.payee)
    for row in rows:
        if row["id"] in claimed:
            continue
        score = 0.5  # amount already matches exactly
        if row["date"] == r.date:
            score += 0.30
        else:
            score += 0.15
        have = normalize_payee(row["payee"] or "")
        if want and have and want == have:
            score += 0.20
        elif want and have and (want in have or have in want):
            score += 0.10
        elif not want and not have:
            score += 0.10
        if score > best_score:
            best_id, best_score = row["id"], score
    if best_id is not None and best_score >= _DUP_THRESHOLD:
        return best_id, round(best_score, 3), "fuzzy"
    return None, 0.0, None


def _find_dup_investment(conn, account_id, r, import_id, claimed) -> Optional[int]:
    """Return the id of an existing investment row this record duplicates, else
    None. Count-aware via ``claimed`` (see :func:`_find_dup_cash`)."""
    if r.fitid:
        rows = conn.execute(
            "SELECT id FROM investment_transactions WHERE account_id=? AND fitid=? "
            "AND (import_id IS NULL OR import_id != ?)",
            (account_id, r.fitid, import_id),
        ).fetchall()
        for row in rows:
            if row["id"] not in claimed:
                return row["id"]
    rows = conn.execute(
        "SELECT id FROM investment_transactions "
        "WHERE account_id=? AND date=? AND action=? AND IFNULL(symbol,'')=? "
        "AND IFNULL(quantity,'')=? AND amount=? AND (import_id IS NULL OR import_id != ?)",
        (account_id, r.date, r.action, r.symbol or "", r.quantity or "", r.amount_cents, import_id),
    ).fetchall()
    for row in rows:
        if row["id"] not in claimed:
            return row["id"]
    return None


# ---------------------------------------------------------------------------
# resolution (get-or-create)
# ---------------------------------------------------------------------------
# Types that count as a generic, unset placeholder: an account auto-created as
# one of these -- e.g. a transfer counter-account resolved with the hardcoded
# "checking" default before the file declares its real type -- may later be
# UPGRADED to a specific type when an AUTHORITATIVE import (the account's OWN
# record, carrying the type the source declared) names it. A specific type is
# never downgraded back to a placeholder.
_PLACEHOLDER_TYPES = {"checking"}


def _resolve_account(conn, name, default_type, cache, *, authoritative=False) -> int:
    """Get-or-create an account by name.

    ``authoritative`` is True only when ``default_type`` comes from the account's
    OWN record (via _import_plain / _import_investment / a transfer's own side),
    which carries the type the source file declared; it is False for the transfer
    counter-account default (a bare "checking" placeholder). On an authoritative
    call whose declared type is specific, a stored "checking" PLACEHOLDER is
    upgraded to that type -- so an account first auto-created as a "checking"
    counter-account self-corrects when a later record declares its real type,
    while a non-authoritative default can never downgrade an already-specific type.
    """
    name = (name or "").strip()
    declared = (default_type or "checking").strip().lower()
    aid = cache.get(name)
    if aid is None:
        row = conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        if row is not None:
            aid = row["id"]
        else:
            aid = ledger.create_account(conn, name, declared or "checking")
            cache[name] = aid
            return aid
        cache[name] = aid
    if authoritative:
        _maybe_upgrade_type(conn, aid, declared)
    return aid


def _maybe_upgrade_type(conn, account_id, declared) -> None:
    """Upgrade an account whose stored type is only the generic "checking"
    placeholder to a specific ``declared`` type. Never downgrades a real type,
    and never runs for a non-authoritative (counter-account) resolution."""
    if not declared or declared in _PLACEHOLDER_TYPES:
        return
    row = conn.execute("SELECT type FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        return
    current = (row["type"] or "").strip().lower()
    if current in _PLACEHOLDER_TYPES and current != declared:
        ledger.update_account(conn, account_id, type=declared)


def _insert_split(conn, txn_id, cat, amt, memo, acct_cache, cat_cache,
                  covered=None, date=None, tag="") -> None:
    """Insert one split leg. A bracketed ``[Account]`` category is a TRANSFER leg
    -- the account is resolved (get-or-create) and stored as the split row's
    ``transfer_account_id`` (category_id NULL). Its MIRROR on the counter-account
    is created here only when the file carries no register for that account on
    this date; when it does, that register supplies the other side and
    fabricating one would double-count it (the same rule
    _import_transfer_pair follows). Anything else is a plain category leg."""
    text = (cat or "").strip()
    if text.startswith("[") and text.endswith("]"):
        name = text[1:-1].strip()
        transfer_account_id = (
            _resolve_account(conn, name, "checking", acct_cache) if name else None
        )
        mirror_id = None
        if (transfer_account_id is not None and date
                and _counterparty_absent(conn, covered, name, date, acct_cache)):
            parent = ledger.get_transaction(conn, txn_id)
            if parent is not None and int(parent["account_id"]) != transfer_account_id:
                mirror_id = ledger._create_split_mirror(
                    conn, parent, transfer_account_id, amt)
        conn.execute(
            "INSERT INTO splits(transaction_id, category_id, transfer_account_id, amount, memo, "
            "transfer_pair_id, tag_id) VALUES (?,?,?,?,?,?,?)",
            (txn_id, None, transfer_account_id, amt, memo or None, mirror_id,
             ledger.tag_id(conn, tag) if tag else None),
        )
    else:
        conn.execute(
            "INSERT INTO splits(transaction_id, category_id, amount, memo, tag_id) "
            "VALUES (?,?,?,?,?)",
            (txn_id, _resolve_category(conn, cat, cat_cache), amt, memo or None,
             ledger.tag_id(conn, tag) if tag else None),
        )


def _resolve_category(conn, path, cache) -> Optional[int]:
    """Cached wrapper over ledger.resolve_category so category rows have a single
    writer (the domain layer) shared with the register UI."""
    path = (path or "").strip()
    if not path:
        return None
    if path in cache:
        return cache[path]
    cid = ledger.resolve_category(conn, path)
    cache[path] = cid
    return cid


def _upsert_payee(conn, payee) -> None:
    payee = (payee or "").strip()
    if not payee:
        return
    conn.execute(
        "INSERT OR IGNORE INTO payees(name, normalized_name) VALUES (?,?)",
        (payee, normalize_payee(payee)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# account <-> institution mapping (institutional ongoing-import; plan G5/G6)
# ---------------------------------------------------------------------------
#
# An ongoing download knows only WHICH institution it pulled from (a
# ``downloads.Institution.key``) and the external account number carried in the
# file (an OFX ``<ACCTID>``, a CSV account column, ...). The legacy import path
# (:func:`_resolve_account`) matched a file to a Mammon account by NAME only,
# which breaks the moment an institution renames an account or two institution
# variants share a display name (e.g. Anytown CU checking vs savings both
# report name "Anytown Credit Union"). This adds a stable
# ``(Institution.key, external_account) -> account_id`` map so imports can
# resolve by institution+account instead of by name.
#
# Storage REUSES the existing ``import_mappings`` table (plan item 1: "reuse the
# ... import_mappings table") rather than inventing a new store. Account-link
# rows are namespaced so they can never collide with categorize.py's
# payee->category rows, which key strictly on a normalized-payee
# ``payee_pattern``:
#   * source        = 'account_link'  -- categorize.py only ever writes
#                     'learned'/'user' and never filters reads by source, so it
#                     never touches these rows;
#   * payee_pattern = '@acct:{institution_key}:{external_account}'  -- a real
#                     payee normalized by record.normalize_payee can never
#                     contain '@' or ':', so categorize.py's ``payee_pattern=?``
#                     lookups can never resolve to one of these keys;
#   * mapped_payee  = the account's display name (human-readable);
#   * hit_count     = the linked account_id (authoritative + rename-proof).
ACCOUNT_LINK_SOURCE = "account_link"
_ACCT_LINK_PREFIX = "@acct:"


def _account_link_pattern(institution_key: str, external_account: str) -> str:
    return f"{_ACCT_LINK_PREFIX}{institution_key.strip()}:{external_account.strip()}"


def _split_link_pattern(pattern: str) -> tuple[str, str]:
    body = pattern[len(_ACCT_LINK_PREFIX):] if pattern.startswith(_ACCT_LINK_PREFIX) else pattern
    key, _, external = body.partition(":")
    return key, external


def set_account_institution_link(
    conn: sqlite3.Connection,
    account_id: int,
    institution_key: str,
    external_account: str,
) -> None:
    """Map ``account_id`` to ``(institution_key, external_account)``.

    Idempotent upsert into the account-link namespace of ``import_mappings``.
    Re-linking the same ``(institution_key, external_account)`` pair just moves
    the link to the given account; never touches a payee->category row.
    """
    institution_key = (institution_key or "").strip()
    external_account = (external_account or "").strip()
    if not (institution_key and external_account):
        raise ValueError("institution_key and external_account are required")
    row = conn.execute("SELECT name FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such account id: {account_id}")
    pattern = _account_link_pattern(institution_key, external_account)
    existing = conn.execute(
        "SELECT id FROM import_mappings WHERE payee_pattern=?", (pattern,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO import_mappings"
            "(payee_pattern, mapped_payee, mapped_category_id, source, hit_count) "
            "VALUES (?,?,?,?,?)",
            (pattern, row["name"], None, ACCOUNT_LINK_SOURCE, account_id),
        )
    else:
        conn.execute(
            "UPDATE import_mappings SET mapped_payee=?, mapped_category_id=NULL, "
            "source=?, hit_count=? WHERE id=?",
            (row["name"], ACCOUNT_LINK_SOURCE, account_id, existing["id"]),
        )
    conn.commit()


def account_id_for_institution(
    conn: sqlite3.Connection, institution_key: str, external_account: str
) -> Optional[int]:
    """The Mammon account_id linked to ``(institution_key, external_account)``, or None."""
    pattern = _account_link_pattern(institution_key or "", external_account or "")
    row = conn.execute(
        "SELECT hit_count FROM import_mappings WHERE payee_pattern=? AND source=?",
        (pattern, ACCOUNT_LINK_SOURCE),
    ).fetchone()
    return int(row["hit_count"]) if row is not None else None


def institution_link_for_account(
    conn: sqlite3.Connection, account_id: int
) -> Optional[tuple[str, str]]:
    """The ``(institution_key, external_account)`` linked to ``account_id``, or None."""
    row = conn.execute(
        "SELECT payee_pattern FROM import_mappings WHERE hit_count=? AND source=?",
        (account_id, ACCOUNT_LINK_SOURCE),
    ).fetchone()
    return _split_link_pattern(row["payee_pattern"]) if row is not None else None


def account_institution_links(conn: sqlite3.Connection) -> list[dict]:
    """Every account-link row, newest-independent, for inspection/tests."""
    out: list[dict] = []
    for r in conn.execute(
        "SELECT payee_pattern, mapped_payee, hit_count FROM import_mappings "
        "WHERE source=? ORDER BY hit_count",
        (ACCOUNT_LINK_SOURCE,),
    ):
        key, external = _split_link_pattern(r["payee_pattern"])
        out.append({
            "account_id": int(r["hit_count"]),
            "institution_key": key,
            "external_account": external,
            "account_name": r["mapped_payee"],
        })
    return out
