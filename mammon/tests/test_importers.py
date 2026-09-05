"""Tests for mammon.importers: QIF/OFX/JSON/CSV parsing, the shared insert path,
fitid + fuzzy dedup, and Quicken transfer-mirror collapse."""
from __future__ import annotations

import pytest

from mammon import db, importers, investments, ledger
from mammon.importers.ofx import parse_ofx_positions, unmapped_investment_actions
from mammon.importers.record import (
    dollars_to_cents,
    normalize_payee,
    parse_date,
)


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _acct_id(conn, name):
    return conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()["id"]


def _acct_type(conn, name):
    return conn.execute("SELECT type FROM accounts WHERE name=?", (name,)).fetchone()["type"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_dollars_to_cents():
    assert dollars_to_cents("-25.00") == -2500
    assert dollars_to_cents("$1,234.56") == 123456
    assert dollars_to_cents("(30.00)") == -3000
    assert dollars_to_cents("") == 0
    assert dollars_to_cents(40) == 4000
    assert dollars_to_cents("=746.36") == 74636   # QIF split balancing "$=" marker


def test_parse_date_shapes():
    assert parse_date("2026-01-05") == "2026-01-05"
    assert parse_date("20260105") == "2026-01-05"        # OFX compact
    assert parse_date("1/5/26") == "2026-01-05"          # US short
    assert parse_date("01/05'26") == "2026-01-05"        # Quicken apostrophe year
    assert parse_date("12/31/1999") == "1999-12-31"


def test_parse_date_iso8601_datetime():
    # ACU download shape: fractional seconds + timezone offset -> calendar day.
    assert parse_date("2026-07-31T00:00:00.000-06:00") == "2026-07-31"
    # UTC 'Z' suffix (unsupported by fromisoformat before 3.11) -> calendar day.
    assert parse_date("2026-07-31T12:34:56Z") == "2026-07-31"
    # Space separator and a plain date still work.
    assert parse_date("2026-07-31 00:00:00") == "2026-07-31"
    assert parse_date("2026-07-31") == "2026-07-31"


def test_normalize_payee():
    assert normalize_payee("SAFEWAY #123") == "SAFEWAY"
    assert normalize_payee("safeway") == "SAFEWAY"


# ---------------------------------------------------------------------------
# QIF cash + transfer collapse
# ---------------------------------------------------------------------------
QIF_BANK = """!Account
NChecking
TBank
^
!Type:Bank
D01/05'26
T-25.00
PSafeway
LGroceries
^
D01/10'26
T200.00
PPaycheck
LSalary
^
D01/15'26
PTransfer to savings
L[Savings]
T-40.00
^
!Account
NSavings
TBank
^
!Type:Bank
D01/15'26
L[Checking]
T40.00
^
"""


def test_qif_bank_import_balances(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "all.qif", QIF_BANK))
    assert res.added == 3           # 2 plain + 1 collapsed transfer
    assert res.transfers == 1
    assert res.duplicates == 0
    checking = _acct_id(conn, "Checking")
    savings = _acct_id(conn, "Savings")
    assert ledger.account_balance(conn, checking) == -25_00 + 200_00 - 40_00
    assert ledger.account_balance(conn, savings) == 40_00
    # transfer moved money, so net worth is just the two real cash flows
    assert ledger.net_worth(conn) == -25_00 + 200_00


def test_qif_transfer_is_single_linked_pair(conn, tmp_path):
    importers.import_file(conn, _write(tmp_path, "all.qif", QIF_BANK))
    # exactly two transfer rows exist, and they are cross-linked
    rows = conn.execute(
        "SELECT id, account_id, amount, transfer_account_id, transfer_pair_id "
        "FROM transactions WHERE transfer_account_id IS NOT NULL ORDER BY amount"
    ).fetchall()
    assert len(rows) == 2
    neg, pos = rows
    assert neg["amount"] == -40_00 and pos["amount"] == 40_00
    assert neg["transfer_pair_id"] == pos["id"]
    assert pos["transfer_pair_id"] == neg["id"]


def test_qif_transfer_payee_kept_on_both_legs(conn, tmp_path):
    # Root cause of the user's "transfers show a BLANK payee": the QIF parser DID
    # capture the P line, but the double-entry collapse path (create_transfer)
    # dropped it. Quicken keeps a normal payee on BOTH legs, so the collapsed
    # pair must carry the P line ("Transfer to savings") on each side -- even
    # though only the Checking leg had a P line in the QIF (the Savings leg had
    # none, so it inherits its mirror's payee).
    importers.import_file(conn, _write(tmp_path, "all.qif", QIF_BANK))
    rows = conn.execute(
        "SELECT payee, amount FROM transactions "
        "WHERE transfer_account_id IS NOT NULL ORDER BY amount"
    ).fetchall()
    assert len(rows) == 2
    assert [r["payee"] for r in rows] == ["Transfer to savings", "Transfer to savings"]


# A transfer between two accounts where each leg carries its OWN Quicken `C`
# flag: the $40 transfer is reconciled (CX) on BOTH sides; the $60 transfer is
# reconciled (CX) on the checking side but only cleared (C*) on the savings side
# -- Quicken reconciles the two legs independently against each account's own
# statement, so the legs may legitimately differ.
QIF_TRANSFER_CLEARED = """!Account
NChecking
TBank
^
!Type:Bank
D01/15'26
PMove to savings
L[Savings]
T-40.00
CX
^
D01/20'26
PPartial move
L[Savings]
T-60.00
CX
^
!Account
NSavings
TBank
^
!Type:Bank
D01/15'26
L[Checking]
T40.00
CX
^
D01/20'26
L[Checking]
T60.00
C*
^
"""


def test_qif_transfer_legs_have_non_blank_clr(conn, tmp_path):
    """Regression: the double-entry collapse (create_transfer) USED to DROP the
    Quicken `C` flag, so every imported transfer arrived cleared=0/reconciled=0
    and its Clr column rendered a bare BLANK amid an otherwise reconciled ledger.
    Both legs must import with a DEFINITE, non-blank status carried from the QIF,
    and the two legs stay INDEPENDENTLY reconcilable (one may be 'R' while its
    mirror is only 'c')."""
    importers.import_file(conn, _write(tmp_path, "xfer.qif", QIF_TRANSFER_CLEARED))
    rows = conn.execute(
        "SELECT amount, cleared, reconciled "
        "FROM transactions WHERE transfer_account_id IS NOT NULL"
    ).fetchall()
    assert len(rows) == 4
    # No leg is NULL and no leg is left in the blank 0/0 state.
    for r in rows:
        assert r["cleared"] is not None and r["reconciled"] is not None
        assert not (r["cleared"] == 0 and r["reconciled"] == 0), (
            f"transfer leg amount={r['amount']} imported with a BLANK Clr status"
        )

    checking = _acct_id(conn, "Checking")
    savings = _acct_id(conn, "Savings")

    def clr(account_id, amount):
        row = conn.execute(
            "SELECT cleared, reconciled FROM transactions "
            "WHERE account_id=? AND amount=?",
            (account_id, amount),
        ).fetchone()
        return (row["cleared"], row["reconciled"])

    # $40 transfer: CX on both sides -> both legs reconciled ('R').
    assert clr(checking, -40_00) == (1, 1)
    assert clr(savings, 40_00) == (1, 1)
    # $60 transfer: per-leg status is NOT mirrored -- checking reconciled (CX ->
    # 'R'), savings only cleared (C* -> 'c'). Neither is blank.
    assert clr(checking, -60_00) == (1, 1)
    assert clr(savings, 60_00) == (1, 0)


# A SINGLE-account QIF -- the common Quicken "export just this account" shape and
# the one the user's bank downloads take. Only one side of each transfer is in the
# file; the review path synthesizes the counterparty mirror on accept. Each row
# carries its OWN Quicken `C` flag: a reconciled transfer leg (CX), a cleared-only
# transfer leg (C*), a reconciled plain deposit (CX), and an uncleared expense.
QIF_SINGLE_ACCOUNT_CLEARED = """!Account
NChecking
TBank
^
!Type:Bank
D01/15'26
PMove to savings
L[Savings]
T-40.00
CX
^
D01/20'26
PPartial move
L[Savings]
T-60.00
C*
^
D01/22'26
PPaycheck
LSalary
T500.00
CX
^
D01/25'26
PGrocery run
LGroceries
T-30.00
^
"""


def test_qif_single_account_import_carries_per_leg_clr(conn, tmp_path):
    """Regression (the user, reported 3x): a SINGLE-account QIF imports through the
    review path (:func:`import_review.save_new`), NOT the multi-account collapse.
    That path USED TO DROP the Quicken `C` flag entirely -- every NEW row (transfer
    or plain) arrived cleared=0/reconciled=0, so a reconciled transfer's Clr=R
    rendered a bare BLANK. Prior fixes only touched the multi-account path
    (importers.core), so its synthetic test stayed green while the user's single-account
    bank imports kept losing the flag.

    Each imported row must keep its OWN status, AND the synthesized counterparty
    mirror leg in the OTHER account must NOT inherit it -- reconciliation is
    per-account and independent (importing/reconciling one side never touches the
    other)."""
    importers.import_single_account(
        conn, account="Checking", finalize=True,
        path=str(_write(tmp_path, "checking.qif", QIF_SINGLE_ACCOUNT_CLEARED)),
    )
    checking = _acct_id(conn, "Checking")
    savings = _acct_id(conn, "Savings")   # get-or-created for the mirror leg

    def clr(account_id, amount):
        row = conn.execute(
            "SELECT cleared, reconciled FROM transactions "
            "WHERE account_id=? AND amount=?",
            (account_id, amount),
        ).fetchone()
        return (row["cleared"], row["reconciled"])

    # Imported (Checking) side keeps each row's OWN Quicken status:
    assert clr(checking, -40_00) == (1, 1)   # CX transfer leg  -> reconciled 'R'
    assert clr(checking, -60_00) == (1, 0)   # C* transfer leg  -> cleared 'c'
    assert clr(checking, 500_00) == (1, 1)   # CX plain deposit -> reconciled 'R'
    assert clr(checking, -30_00) == (0, 0)   # no C line        -> blank/uncleared

    # PER-ACCOUNT independence: the synthesized Savings mirror legs are NOT forced
    # to the Checking legs' status -- they stay at their own default (0/0) and
    # reconcile independently when Savings is itself imported/reconciled.
    assert clr(savings, 40_00) == (0, 0)
    assert clr(savings, 60_00) == (0, 0)


def test_qif_categories_created(conn, tmp_path):
    importers.import_file(conn, _write(tmp_path, "all.qif", QIF_BANK))
    names = {r["name"] for r in conn.execute("SELECT name FROM categories").fetchall()}
    assert {"Groceries", "Salary"} <= names


# ---------------------------------------------------------------------------
# USER BUG (12/08/2000 mortgage): a QIF cash split that carries its OWN [House]
# principal leg + an 'Int Exp' interest leg. Quicken echoes the first split
# category in the top-level L line (L[House]), which USED to mark the whole
# payment a transfer -- so it displayed as a lone [House], hiding the split and
# breaking reconciliation. It must import as --Split-- with BOTH legs visible,
# the [House] principal leg reciprocal with the House account's own mirror.
# ---------------------------------------------------------------------------
QIF_SPLIT_TRANSFER = """!Account
NAnytown CU Ck
TBank
^
!Type:Bank
D12/08/2000
PCrossland Mortgage Corp
T-829.56
L[House]
S[House]
$-120.46
Eprincipal
SInt Exp
$-709.10
Einterest
^
!Account
NHouse
TOth A
^
!Type:Oth A
D12/08/2000
L[Anytown CU Ck]
T120.46
^
"""


def test_qif_split_with_transfer_leg_displays_as_split(conn, tmp_path):
    importers.import_file(conn, _write(tmp_path, "mortgage.qif", QIF_SPLIT_TRANSFER))
    checking = _acct_id(conn, "Anytown CU Ck")
    house = _acct_id(conn, "House")
    pay = conn.execute(
        "SELECT id FROM transactions WHERE payee='Crossland Mortgage Corp'"
    ).fetchone()
    txn = ledger.get_transaction(conn, pay["id"])
    # displays as --Split--, NOT a lone [House]; the split owns the transfer.
    assert ledger.category_display(conn, txn) == ledger.SPLIT_LABEL
    assert txn["transfer_account_id"] is None
    # both legs surface: principal $120.46 [House] + interest $709.10 'Int Exp'.
    legs = [(l["category_label"], l["amount"]) for l in ledger.get_splits(conn, pay["id"])]
    assert legs == [("[House]", -120_46), ("Int Exp", -709_10)]
    # the House side keeps its own reciprocal $120.46 [Anytown CU Ck] mirror,
    # so the principal leg reconciles across the two accounts.
    hrows = ledger.register_rows(conn, house)
    assert len(hrows) == 1
    assert hrows[0]["amount"] == 120_46
    assert hrows[0]["category_label"] == "[Anytown CU Ck]"
    # balances: checking down the full payment, house up the principal.
    assert ledger.account_balance(conn, checking) == -829_56
    assert ledger.account_balance(conn, house) == 120_46


# A Acme-style paycheck (from Mammon_2000.QIF): a DEPOSIT split whose 401(k)
# deferral leg is a bracketed transfer ``S[Acme 401K]`` to an INVESTMENT
# account (Quicken ``TPort``), not a loan/asset account. This is the same generic
# bracketed-split-leg path as the mortgage ``[House]`` leg above -- there is no
# separate "paycheck" code path, and the counter-account's TYPE (investment vs
# asset) is irrelevant to leg resolution: ``_insert_split`` resolves any
# ``[Account]`` split leg to a ``transfer_account_id`` so it renders ``[Account]``
# rather than a BLANK category. Regression: these legs used to import blank.
QIF_PAYCHECK_401K = """!Account
NAnytown CU Ck
TBank
^
!Type:Bank
D01/14/2000
PAcme
T2722.40
LSalary
SSalary
$3232.00
STax:Fed
$-380.32
S[Acme 401K]
$-129.28
Edeferral
^
!Account
NAcme 401K
TInvst
^
!Type:Invst
D01/14/2000
NXIn
L[Anytown CU Ck]
T129.28
^
"""


def test_qif_paycheck_401k_split_leg_displays_as_transfer(conn, tmp_path):
    importers.import_file(conn, _write(tmp_path, "paycheck.qif", QIF_PAYCHECK_401K))
    checking = _acct_id(conn, "Anytown CU Ck")
    k401 = _acct_id(conn, "Acme 401K")
    # the 401(k) transfer target is an INVESTMENT account, not a bank/asset one.
    assert _acct_type(conn, "Acme 401K") == "investment"
    pay = conn.execute("SELECT id FROM transactions WHERE payee='Acme'").fetchone()
    txn = ledger.get_transaction(conn, pay["id"])
    # displays as --Split--, NOT a lone [Acme 401K]; the split owns the legs.
    assert ledger.category_display(conn, txn) == ledger.SPLIT_LABEL
    assert txn["transfer_account_id"] is None
    legs = ledger.get_splits(conn, pay["id"])
    # every leg surfaces a label -- none is BLANK (the bug: 401(k) leg imported
    # with both category_id and transfer_account_id NULL, rendering '').
    assert all(l["category_label"] for l in legs)
    by_label = {l["category_label"]: l for l in legs}
    assert "[Acme 401K]" in by_label
    k = by_label["[Acme 401K]"]
    # the deferral leg is a TRANSFER (transfer_account_id set, category_id NULL),
    # reciprocal with the 401(k) account -- exactly like the [House] leg above.
    assert k["amount"] == -129_28
    assert k["transfer_account_id"] == k401
    assert k["category_id"] is None
    # the other legs stay plain categories; full leg set is exact.
    assert [(l["category_label"], l["amount"]) for l in legs] == [
        ("Salary", 3232_00),
        ("Tax:Fed", -380_32),
        ("[Acme 401K]", -129_28),
    ]
    # paycheck is a deposit: checking up the net; the 401(k) side keeps its own
    # reciprocal XIn (no fabricated mirror on import).
    assert ledger.account_balance(conn, checking) == 2722_40


# A real pattern from Mammon_1997.QIF: a property purchase is a MULTI-WAY transfer
# whose legs carry DIFFERENT amounts -- Checking pays only the down payment
# (-22,653.87), the asset account records the full price (+118,000), and the loan
# records the mortgage (-94,400). These are NOT equal-and-opposite mirrors, so
# each account must keep ONLY its own leg (a one-sided transfer with no mirror);
# fabricating the counterparty side double-counted checking (it used to be debited
# the full 118,000 on top of its real 22,653.87). Symmetric transfers still
# collapse to a single linked pair (see test_qif_transfer_is_single_linked_pair).
QIF_ASYMMETRIC_TRANSFER = """!Account
NChecking
TBank
^
!Type:Bank
D05/23'97
PMountain West Title
L[CondoAsset]
T-22653.87
^
!Account
NCondoAsset
TOth A
^
!Type:Oth A
D05/23'97
PMountain West Title
L[Checking]
T118000.00
^
!Account
NCondoLoan
TOth L
^
!Type:Oth L
D05/23'97
PMountain West Title
L[Checking]
T-94400.00
^
"""


def test_qif_asymmetric_multiway_transfer_keeps_own_leg(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "buy.qif", QIF_ASYMMETRIC_TRANSFER))
    assert res.errors == 0
    # Each account's balance is exactly its own register line -- no double count.
    assert ledger.account_balance(conn, _acct_id(conn, "Checking")) == -22653_87
    assert ledger.account_balance(conn, _acct_id(conn, "CondoAsset")) == 118000_00
    assert ledger.account_balance(conn, _acct_id(conn, "CondoLoan")) == -94400_00
    # The legs are one-sided: linked to a counter-account but with NO mirror pair.
    legs = conn.execute(
        "SELECT amount, transfer_account_id, transfer_pair_id FROM transactions "
        "WHERE transfer_account_id IS NOT NULL"
    ).fetchall()
    assert len(legs) == 3
    assert all(row["transfer_pair_id"] is None for row in legs)
    # CondoAsset/CondoLoan are first seen as "checking" transfer counter-accounts
    # (referenced by Checking) and then declared Oth A / Oth L by their own blocks
    # later in the SAME file: the authoritative declaration upgrades the placeholder.
    assert _acct_type(conn, "CondoAsset") == "asset"
    assert _acct_type(conn, "CondoLoan") == "liability"
    # Re-importing the same file is idempotent (each leg dedups, none doubles).
    res2 = importers.import_file(conn, _write(tmp_path, "buy.qif", QIF_ASYMMETRIC_TRANSFER))
    assert res2.added == 0 and res2.duplicates == 3
    assert ledger.account_balance(conn, _acct_id(conn, "Checking")) == -22653_87


# Account-type-on-reimport: a placeholder account first auto-created as the generic
# "checking" default (because it was seen only as a transfer counter-account)
# self-corrects to its real type when a later import carries its OWN declared type;
# a non-authoritative counter-account reference never downgrades a specific type.
QIF_LOAN_COUNTER_ONLY = """!Account
NChecking
TBank
^
!Type:Bank
D01/15'97
PMortgage Co
L[HomeLoan]
T-1000.00
^
"""

QIF_LOAN_OWN = """!Account
NHomeLoan
TOth L
^
!Type:Oth L
D01/20'97
PInterest
LInterest Exp
T-50.00
^
"""


def test_account_type_upgrades_on_reimport(conn, tmp_path):
    # File 1 names HomeLoan only as a transfer counter-account -> auto-created with
    # the generic "checking" placeholder.
    importers.import_file(conn, _write(tmp_path, "chk.qif", QIF_LOAN_COUNTER_ONLY))
    assert _acct_type(conn, "HomeLoan") == "checking"
    # File 2 carries HomeLoan's OWN record declaring Oth L -> reimport upgrades the
    # placeholder to the specific type (liability).
    importers.import_file(conn, _write(tmp_path, "loan.qif", QIF_LOAN_OWN))
    assert _acct_type(conn, "HomeLoan") == "liability"
    # A genuine bank account is unaffected (its declared type is not a placeholder).
    assert _acct_type(conn, "Checking") == "checking"


def test_transfer_counter_default_does_not_downgrade_specific_type(conn, tmp_path):
    # HomeLoan is first declared a liability by its own record.
    importers.import_file(conn, _write(tmp_path, "loan.qif", QIF_LOAN_OWN))
    assert _acct_type(conn, "HomeLoan") == "liability"
    # A later file references [HomeLoan] only as a transfer counter-account (a
    # non-authoritative "checking" default) -> the specific type must NOT downgrade.
    importers.import_file(conn, _write(tmp_path, "chk.qif", QIF_LOAN_COUNTER_ONLY))
    assert _acct_type(conn, "HomeLoan") == "liability"


# A real record from Mammon_1997.QIF: a "Refinance of Home" entry whose top-level
# L is a transfer account ([ANYTOWN House Loan2]) but whose amount is ZERO (the
# money is broken out across split lines). create_transfer rejects a non-positive
# amount, so a zero-amount transfer must DEGRADE to a plain row -- keeping its
# splits -- instead of crashing the whole import (regression: it used to raise
# "transfer amount must be a positive number of cents").
QIF_ZERO_TRANSFER = """!Account
NChecking
TBank
^
!Type:Bank
D01/10'26
T-25.00
PSafeway
LGroceries
^
D01/04'10
T0.00
PRefinance of Home
L[House Loan]
SMortgage
$-1000.00
SEscrow
$1000.00
^
"""


def test_qif_zero_amount_transfer_degrades_to_plain(conn, tmp_path):
    # The import must COMPLETE (no crash) and keep the zero-amount refinance row.
    res = importers.import_file(conn, _write(tmp_path, "zt.qif", QIF_ZERO_TRANSFER))
    assert res.errors == 0
    assert res.added == 2                       # the Safeway row + the refinance row
    # the refinance landed as a plain (non-transfer) row with amount 0 and its splits
    row = conn.execute(
        "SELECT id, amount, transfer_account_id FROM transactions WHERE payee=?",
        ("Refinance of Home",),
    ).fetchone()
    assert row is not None
    assert row["amount"] == 0
    assert row["transfer_account_id"] is None   # degraded to plain, not a transfer
    n_splits = conn.execute(
        "SELECT COUNT(*) c FROM splits WHERE transaction_id=?", (row["id"],)
    ).fetchone()["c"]
    assert n_splits == 2                         # split detail preserved


# A real Quicken QIF export (verified against Mammon_1995_1996.QIF) leads with
# metadata sections - !Type:Tag and !Type:Cat, whose entries carry a 'D' that is a
# DESCRIPTION, not a date - and an !Option:AutoSwitch account list, and writes an
# opening balance as a self-transfer L[SameAccount]. Regression: the importer must
# SKIP metadata sections (previously it read a category's description as a txn date
# and crashed on parse_date('Book series')) and never make the self-transfer a
# transfer (previously a source of phantom accounts). Into an EMPTY account that
# row is the account's opening balance (what Quicken means by it, and what
# mammon.export writes); into a populated one it stays the plain row it always
# was, so a ledger built under the older reading re-imports unchanged.
QIF_REAL_SHAPE = (
    "!Type:Tag\n"
    "NVacation\n^\n"
    "NEscaping the Virus\nDBook series\n^\n"
    "!Type:Cat\n"
    "NGroceries\nDFood at home\nE\n^\n"
    "NSalary\nI\n^\n"
    "!Option:AutoSwitch\n"
    "!Account\nNChecking\nTBank\n^\n"
    "!Account\nNSavings\nTBank\n^\n"
    "!Clear:AutoSwitch\n"
    "!Account\nNChecking\nTBank\n^\n"
    "!Type:Bank\n"
    "D12/31/95\nT112.94\nPOpening Balance\nL[Checking]\n^\n"
    "D01/05/96\nT-25.00\nPSafeway\nLGroceries\n^\n"
)


def test_qif_skips_metadata_and_adopts_the_opening_balance(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "real.qif", QIF_REAL_SHAPE))
    # only the one real bank transaction imports; Tag/Cat metadata is skipped and
    # the opening-balance self-transfer becomes the new account's opening
    # balance -- not a row, not a transfer.
    assert res.added == 1
    assert res.errors == 0
    assert res.transfers == 0
    assert conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 1
    acct = conn.execute("SELECT opening_balance, opening_date FROM accounts "
                        "WHERE name='Checking'").fetchone()
    assert (acct["opening_balance"], acct["opening_date"]) == (112_94, "1995-12-31")
    chk = _acct_id(conn, "Checking")
    assert ledger.account_balance(conn, chk) == 112_94 - 25_00
    # Savings was declared in the AutoSwitch list but has no txns -> not created
    assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1
    # The same file again says the same opening balance: skipped, nothing doubles.
    res2 = importers.import_file(conn, _write(tmp_path, "real2.qif", QIF_REAL_SHAPE))
    assert res2.added == 0 and res2.duplicates >= 1
    assert ledger.account_balance(conn, chk) == 112_94 - 25_00
    # A ledger that holds the opening balance as a +row (the older reading)
    # keeps it: the self-transfer stays a plain row and dedups against it.
    ledger.set_opening_balance(conn, chk, 0)
    ledger.add_transaction(conn, chk, "1995-12-31", 112_94, payee="Opening Balance")
    res3 = importers.import_file(conn, _write(tmp_path, "real3.qif", QIF_REAL_SHAPE))
    assert res3.added == 0
    assert ledger.account_balance(conn, chk) == 112_94 - 25_00
    assert conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 2


def test_qif_reimport_dedups(conn, tmp_path):
    p = _write(tmp_path, "all.qif", QIF_BANK)
    importers.import_file(conn, p)
    before = ledger.net_worth(conn)
    n_before = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    res2 = importers.import_file(conn, p)
    assert res2.added == 0
    assert res2.duplicates >= 3
    assert conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == n_before
    assert ledger.net_worth(conn) == before


def test_qif_reimport_flips_transfer_to_plain_dedups(conn, tmp_path):
    # WATCHDOG regression: a payment first imported as a TRANSFER leg (bracketed
    # [Mortgage] category, so transfer_account_id is set) and later re-imported
    # from a source that classifies the SAME row as a PLAIN category must dedup,
    # not double-count. _find_dup_cash used to restrict fuzzy candidates to
    # transfer_account_id IS NULL, so the plain re-import never saw the existing
    # transfer row and inserted a duplicate -- this drove data/mammon.db's
    # Anytown CU Ck ~$70k off (a mortgage payment counted twice).
    xfer = (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D07/01'97\nPNationsBanc Mortgage\nT-1652.00\nL[Mortgage]\n^\n"
        "!Account\nNMortgage\nTOth L\n^\n!Type:Oth L\n"
        "D07/01'97\nPNationsBanc Mortgage\nT1652.00\nL[Checking]\n^\n"
    )
    res1 = importers.import_file(conn, _write(tmp_path, "xfer.qif", xfer))
    assert res1.transfers == 1
    checking = _acct_id(conn, "Checking")
    # the existing Checking row is a transfer (transfer_account_id set)
    row = conn.execute(
        "SELECT transfer_account_id FROM transactions WHERE account_id=?", (checking,)
    ).fetchone()
    assert row["transfer_account_id"] is not None
    assert ledger.account_balance(conn, checking) == -1652_00
    before_nw = ledger.net_worth(conn)
    n_before = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (checking,)
    ).fetchone()["c"]

    # re-import the SAME payment, now classified PLAIN (no bracket) -- must dedup
    plain = (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D07/01'97\nPNationsBanc Mortgage\nT-1652.00\nLMortgage:Payment\n^\n"
    )
    res2 = importers.import_file(conn, _write(tmp_path, "plain.qif", plain))
    assert res2.added == 0
    assert res2.duplicates == 1
    # no duplicate row, balance and net worth unchanged
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (checking,)
    ).fetchone()["c"] == n_before
    assert ledger.account_balance(conn, checking) == -1652_00
    assert ledger.net_worth(conn) == before_nw


def test_qif_same_file_keeps_legit_identical_rows(conn, tmp_path):
    # two identical same-day purchases must BOTH import (source already deduped)
    qif = (
        "!Account\nNCard\nTCCard\n^\n!Type:CCard\n"
        "D02/02'26\nT-5.00\nPCoffee\nLDining\n^\n"
        "D02/02'26\nT-5.00\nPCoffee\nLDining\n^\n"
    )
    res = importers.import_file(conn, _write(tmp_path, "dup.qif", qif))
    assert res.added == 2
    assert ledger.account_balance(conn, _acct_id(conn, "Card")) == -10_00


# ---------------------------------------------------------------------------
# QIF investment
# ---------------------------------------------------------------------------
QIF_INVST = """!Account
NBrokerage
TInvst
^
!Type:Invst
D02/01'26
NBuy
YAAPL
Q10
I150.00
T1500.00
O4.95
^
D03/01'26
NDiv
YAAPL
T12.50
^
"""


def test_qif_investment_import(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "inv.qif", QIF_INVST))
    assert res.investments == 2
    rows = conn.execute(
        "SELECT action, symbol, quantity, price, amount, commission "
        "FROM investment_transactions ORDER BY date"
    ).fetchall()
    buy, div = rows
    assert buy["action"] == "Buy" and buy["symbol"] == "AAPL"
    assert buy["quantity"] == "10" and buy["price"] == "150.00"
    assert buy["amount"] == 1500_00 and buy["commission"] == 495
    assert div["action"] == "Div" and div["amount"] == 12_50


def test_qif_txn_carried_price_recorded_in_history(conn, tmp_path):
    """A price carried on an investment transaction (QIF ``I`` field) must be
    captured into price_history so valuation can use the last-known price for
    funds that Quicken exports with NO separate !Type:Prices series. The Div row
    carries no price, so it produces no price_history row."""
    from decimal import Decimal
    from mammon import investments

    importers.import_file(conn, _write(tmp_path, "inv.qif", QIF_INVST))
    rows = conn.execute(
        "SELECT date, close_price, source FROM price_history WHERE symbol='AAPL' ORDER BY date"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-02-01"
    assert Decimal(rows[0]["close_price"]) == Decimal("150.00")
    assert rows[0]["source"] == "txn"   # any source format, not just QIF

    # and the last-known transaction price flows through _resolve_price / valuation
    aid = _acct_id(conn, "Brokerage")
    assert investments.latest_price(conn, "AAPL", "2026-08-01") == Decimal("150.00")
    assert investments._resolve_price(conn, "AAPL", "2026-08-01", None, aid) == Decimal("150.00")


def test_qif_explicit_quote_beats_txn_price_same_date(conn, tmp_path):
    """An authoritative !Type:Prices quote must win over a transaction-carried
    price for the same (symbol, date): txn prices are recorded DO-NOTHING, the
    explicit quote DO-UPDATE."""
    from decimal import Decimal
    from mammon import investments

    qif = (
        "!Type:Security\nNGrowth Fund\nSGRWX\nTMutual Fund\n^\n"
        "!Account\nNRoth IRA\nTInvst\n^\n"
        "!Type:Invst\nD01/15'97\nNBuy\nYGrowth Fund\nQ100\nI10.00\nT1000.00\n^\n"
        "!Type:Prices\n\"GRWX\",11.25,\"01/15'97\"\n^\n"
    )
    importers.import_file(conn, _write(tmp_path, "clash.qif", qif))
    # same (symbol, date) 1997-01-15: explicit 11.25 quote wins over txn 10.00
    assert investments.latest_price(conn, "Growth Fund", "1997-01-15") == Decimal("11.25")
    row = conn.execute(
        "SELECT source FROM price_history WHERE symbol='Growth Fund' AND date='1997-01-15'"
    ).fetchone()
    assert row["source"] == "qif"


# A Quicken export with a security master (!Type:Security) + full price history
# (!Type:Prices) alongside the investment register. Investment transactions name
# the security by NAME (Y field), while the price section keys by ticker SYMBOL --
# the importer must bridge the two so holdings can be priced. Includes a legacy
# fractional quote ("13 1/2" = 13.50) as real pre-decimalization exports carry.
QIF_INVST_WITH_PRICES = """!Type:Security
NGrowth Fund
SGRWX
TMutual Fund
^
!Account
NRoth IRA
TInvst
^
!Type:Invst
D01/15'97
NBuy
YGrowth Fund
Q100
I10.00
T1000.00
^
D06/15'97
NBuy
YGrowth Fund
Q50
I12.00
T600.00
^
!Type:Prices
"GRWX",11.00," 3/31'97"
^
!Type:Prices
"GRWX",13 1/2,"12/31'97"
^
"""


def test_qif_security_price_import_and_valuation(conn, tmp_path):
    from mammon import investments

    res = importers.import_file(conn, _write(tmp_path, "priced.qif", QIF_INVST_WITH_PRICES))
    assert res.investments == 2
    aid = _acct_id(conn, "Roth IRA")

    # holdings were rebuilt from the investment transactions (keyed by the name)
    h = investments.get_holding(conn, aid, "Growth Fund")
    assert h["quantity"] == "150"

    # prices are recorded under the security NAME (mapped from the ticker) and the
    # legacy fraction "13 1/2" parsed to 13.50
    from decimal import Decimal
    assert investments.latest_price(conn, "Growth Fund", "1997-06-01") == Decimal("11")
    assert investments.latest_price(conn, "Growth Fund", "1997-12-31") == Decimal("13.5")

    # valuation at year-end: 150 * 13.50 securities, cash = -(1000+600) buys
    val = investments.account_valuation(conn, aid, as_of="1997-12-31")
    assert val.securities == 2025_00
    assert val.cash == -1600_00
    assert val.total == 425_00
    assert val.unpriced == []

    # the account list / net worth show the market valuation, not cash alone
    assert investments.display_balance(conn, aid, as_of="1997-12-31") == 425_00


def test_qif_price_import_is_idempotent(conn, tmp_path):
    # re-importing the same securities+prices dedups transactions and upserts
    # prices (no duplicate price_history rows, holdings unchanged)
    p = _write(tmp_path, "priced.qif", QIF_INVST_WITH_PRICES)
    importers.import_file(conn, p)
    n1 = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    res2 = importers.import_file(conn, p)
    assert res2.investments == 0 and res2.duplicates == 2
    n2 = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    # 2 explicit !Type:Prices quotes + 2 transaction-carried prices (I10.00 @
    # 1997-01-15, I12.00 @ 1997-06-15, source 'txn'); re-import upserts, never
    # duplicates.
    assert n1 == n2 == 4
    aid = _acct_id(conn, "Roth IRA")
    from mammon import investments
    assert investments.get_holding(conn, aid, "Growth Fund")["quantity"] == "150"


# A single security priced across two eras: a Buy carries its own I-price, then
# two !Type:Prices quotes land at two DIFFERENT dates. Proves price_history rows
# carry the per-record date parsed from the QIF (via parse_date), so valuation is
# historically accurate rather than pinned to one import-time/fixed date.
QIF_MULTI_DATE_PRICES = """!Type:Security
NIndex Fund
SIDXX
TMutual Fund
^
!Account
NRoth IRA
TInvst
^
!Type:Invst
D01/01'19
NBuy
YIndex Fund
Q10
I50.00
T500.00
^
!Type:Prices
"IDXX",100.00,"06/30'19"
^
!Type:Prices
"IDXX",200.00,"12/31'21"
^
"""


def test_qif_price_record_date_flows_per_record_to_history(conn, tmp_path):
    """REGRESSION: each QIF price record's OWN date -- parsed through the
    parse_date chokepoint -- must land in price_history.date, so investment
    valuations (e.g. tickerless 401k funds) are historically accurate and NOT
    frozen to a single hardcoded/import-time date. Two !Type:Prices quotes at
    distinct dates produce two rows carrying those two distinct dates; the Buy's
    I-price files under the transaction's own date; and valuation as of each era
    resolves that era's price."""
    from decimal import Decimal
    from mammon import investments

    importers.import_file(conn, _write(tmp_path, "hist.qif", QIF_MULTI_DATE_PRICES))

    # The Quicken apostrophe dates flow through parse_date into the date column
    # verbatim -- proving no hardcoded/import-time date is substituted.
    assert parse_date("06/30'19") == "2019-06-30"
    assert parse_date("12/31'21") == "2021-12-31"
    quotes = conn.execute(
        "SELECT date, close_price FROM price_history "
        "WHERE symbol='Index Fund' AND source='qif' ORDER BY date"
    ).fetchall()
    assert [r["date"] for r in quotes] == ["2019-06-30", "2021-12-31"]
    assert [Decimal(r["close_price"]) for r in quotes] == [Decimal("100"), Decimal("200")]

    # The transaction-carried price files under the transaction's OWN date, too.
    txn = conn.execute(
        "SELECT date, close_price FROM price_history "
        "WHERE symbol='Index Fund' AND source='txn'"
    ).fetchone()
    assert txn["date"] == "2019-01-01"
    assert Decimal(txn["close_price"]) == Decimal("50")

    # Historical accuracy: each record's price is used only on/after its own date,
    # so as-of valuation walks the real per-record timeline -- not one fixed date.
    assert investments.latest_price(conn, "Index Fund", "2019-03-01") == Decimal("50")
    assert investments.latest_price(conn, "Index Fund", "2019-06-30") == Decimal("100")
    assert investments.latest_price(conn, "Index Fund", "2020-01-01") == Decimal("100")
    assert investments.latest_price(conn, "Index Fund", "2022-01-01") == Decimal("200")


# ---------------------------------------------------------------------------
# OFX / QFX
# ---------------------------------------------------------------------------
OFX_BANK = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKACCTFROM><ACCTID>111222333</ACCTID></BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260105
<TRNAMT>-25.00
<FITID>A1
<NAME>SAFEWAY
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260110
<TRNAMT>200.00
<FITID>A2
<NAME>PAYROLL
</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def test_ofx_bank_import_and_fitid_dedup(conn, tmp_path):
    p = _write(tmp_path, "wf.ofx", OFX_BANK)
    res = importers.import_file(conn, p, account="WF Checking")
    assert res.added == 2
    wf = _acct_id(conn, "WF Checking")
    assert ledger.account_balance(conn, wf) == -25_00 + 200_00
    # re-import: both rows carry a fitid, so both are exact duplicates
    res2 = importers.import_file(conn, p, account="WF Checking")
    assert res2.added == 0 and res2.duplicates == 2


QIF_CHECK = """!Account
NChecking
TBank
^
!Type:Bank
D01/20'26
T-125.00
N1234
PWater Utility
LUtilities
^
"""


def test_qif_check_number_captured_into_num(conn, tmp_path):
    # A cash-section 'N' line is the CHECK NUMBER (not the investment 'action');
    # it must land in the transaction's Num field so the register shows it.
    importers.import_file(conn, _write(tmp_path, "chk.qif", QIF_CHECK))
    row = conn.execute(
        "SELECT num FROM transactions WHERE payee=?", ("Water Utility",)
    ).fetchone()
    assert row["num"] == "1234"


OFX_CHECK = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKACCTFROM><ACCTID>555444333</ACCTID></BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CHECK
<DTPOSTED>20260120
<TRNAMT>-125.00
<FITID>C1
<CHECKNUM>4567
<NAME>WATER UTILITY
</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def test_ofx_checknum_captured_into_num(conn, tmp_path):
    # OFX CHECKNUM maps to the transaction's Num field on import.
    importers.import_file(conn, _write(tmp_path, "c.ofx", OFX_CHECK), account="Chk")
    row = conn.execute(
        "SELECT num FROM transactions WHERE fitid=?", ("C1",)
    ).fetchone()
    assert row["num"] == "4567"


OFX_INVST = """<OFX>
<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
<INVACCTFROM><ACCTID>Z999</ACCTID></INVACCTFROM>
<INVTRANLIST>
<BUYSTOCK>
<INVBUY>
<INVTRAN><FITID>B1<DTTRADE>20260201</INVTRAN>
<SECID><TICKER>AAPL</TICKER></SECID>
<UNITS>10<UNITPRICE>150.00<TOTAL>-1500.00
</INVBUY>
</BUYSTOCK>
<INCOME>
<INVTRAN><FITID>B2<DTTRADE>20260301</INVTRAN>
<SECID><TICKER>AAPL</TICKER></SECID>
<INCOMETYPE>DIV
<TOTAL>12.50
</INCOME>
</INVTRANLIST>
</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>
</OFX>
"""


def test_ofx_investment_import(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "b.qfx", OFX_INVST), account="Broker")
    assert res.investments == 2
    rows = conn.execute(
        "SELECT action, symbol, quantity, amount FROM investment_transactions ORDER BY date"
    ).fetchall()
    buy, div = rows
    assert buy["action"] == "Buy" and buy["symbol"] == "AAPL"
    assert buy["quantity"] == "10" and buy["amount"] == -1500_00
    assert div["action"] == "Div" and div["amount"] == 12_50


# ---------------------------------------------------------------------------
# OFX investment ACTION MAPPING (Split/Transfer/CloseOpt/RetOfCap/MargInt/
# InvExpense) + the unmapped-action audit. Each formerly defaulted to a silent
# cash-in / no-share-change, corrupting holdings for IB/TDA/Schwab feeds.
# ---------------------------------------------------------------------------
def _ofx_inv(inner: str) -> str:
    """Wrap an <INVTRANLIST> body in a minimal single-account OFX statement."""
    return (
        "<OFX>\n"
        "<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>\n"
        "<INVACCTFROM><ACCTID>Z1</ACCTID></INVACCTFROM>\n"
        "<INVTRANLIST>\n"
        f"{inner}\n"
        "</INVTRANLIST>\n"
        "</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>\n"
        "</OFX>\n"
    )


_OFX_BUY_AAPL = """<BUYSTOCK>
<INVBUY>
<INVTRAN><FITID>B0<DTTRADE>20260101</INVTRAN>
<SECID><TICKER>AAPL</TICKER></SECID>
<UNITS>10<UNITPRICE>150.00<TOTAL>-1500.00
</INVBUY>
</BUYSTOCK>"""


def test_ofx_split_scales_shares_not_cash(conn, tmp_path):
    body = _OFX_BUY_AAPL + """
<SPLIT>
<INVTRAN><FITID>SP1<DTTRADE>20260601</INVTRAN>
<SECID><TICKER>AAPL</TICKER></SECID>
<OLDUNITS>10<NEWUNITS>20<NUMERATOR>2<DENOMINATOR>1<FRACCASH>0.00
</SPLIT>"""
    importers.import_file(conn, _write(tmp_path, "s.qfx", _ofx_inv(body)), account="Broker")
    aid = _acct_id(conn, "Broker")
    split = conn.execute(
        "SELECT action, quantity, amount FROM investment_transactions WHERE fitid='SP1'"
    ).fetchone()
    assert split["action"] == "StkSplit"
    assert split["quantity"] == "20"        # 'new shares per 10 old' for a 2:1 split
    assert split["amount"] == 0
    holds = {h["symbol"]: h for h in investments.list_holdings(conn, aid)}
    assert holds["AAPL"]["quantity"] == "20"        # 10 shares doubled
    assert holds["AAPL"]["cost_basis"] == 1500_00   # basis unchanged by a split
    assert investments.investment_cash(conn, aid) == -1500_00   # split moves no cash


def test_ofx_transfer_moves_shares_not_cash(conn, tmp_path):
    body = """<TRANSFER>
<INVTRAN><FITID>T1<DTTRADE>20260301</INVTRAN>
<SECID><TICKER>MSFT</TICKER></SECID>
<TFERACTION>IN<UNITS>5<UNITPRICE>100.00
</TRANSFER>
<TRANSFER>
<INVTRAN><FITID>T2<DTTRADE>20260401</INVTRAN>
<SECID><TICKER>MSFT</TICKER></SECID>
<TFERACTION>OUT<UNITS>2
</TRANSFER>"""
    importers.import_file(conn, _write(tmp_path, "t.qfx", _ofx_inv(body)), account="Broker")
    aid = _acct_id(conn, "Broker")
    rows = {r["fitid"]: r for r in conn.execute(
        "SELECT fitid, action, quantity, amount FROM investment_transactions"
    ).fetchall()}
    assert rows["T1"]["action"] == "ShrsIn"
    assert rows["T1"]["quantity"] == "5" and rows["T1"]["amount"] == 0
    assert rows["T2"]["action"] == "ShrsOut"
    assert rows["T2"]["quantity"] == "2" and rows["T2"]["amount"] == 0
    holds = {h["symbol"]: h for h in investments.list_holdings(conn, aid)}
    assert holds["MSFT"]["quantity"] == "3"             # 5 in - 2 out
    assert investments.investment_cash(conn, aid) == 0  # a securities transfer is cash-neutral


def test_ofx_closureopt_removes_option_units(conn, tmp_path):
    body = """<BUYOTHER>
<INVBUY>
<INVTRAN><FITID>O1<DTTRADE>20260201</INVTRAN>
<SECID><UNIQUEID>OPT123</UNIQUEID></SECID>
<UNITS>10<UNITPRICE>2.00<TOTAL>-2000.00
</INVBUY>
</BUYOTHER>
<CLOSUREOPT>
<INVTRAN><FITID>O2<DTTRADE>20260301</INVTRAN>
<SECID><UNIQUEID>OPT123</UNIQUEID></SECID>
<OPTACTION>EXPIRE<UNITS>10<SHPERCTRCT>100
</CLOSUREOPT>"""
    importers.import_file(conn, _write(tmp_path, "c.qfx", _ofx_inv(body)), account="Broker")
    aid = _acct_id(conn, "Broker")
    close = conn.execute(
        "SELECT action, symbol, quantity, amount FROM investment_transactions WHERE fitid='O2'"
    ).fetchone()
    assert close["action"] == "RemoveShares"
    assert close["symbol"] == "OPT123"
    assert close["quantity"] == "10" and close["amount"] == 0
    assert investments.list_holdings(conn, aid) == []          # position fully closed
    assert investments.investment_cash(conn, aid) == -2000_00  # closing moves no cash


def test_ofx_return_of_capital_reduces_basis_and_is_cash_in(conn, tmp_path):
    body = """<BUYSTOCK>
<INVBUY>
<INVTRAN><FITID>R0<DTTRADE>20260101</INVTRAN>
<SECID><TICKER>VTI</TICKER></SECID>
<UNITS>100<UNITPRICE>50.00<TOTAL>-5000.00
</INVBUY>
</BUYSTOCK>
<RETOFCAP>
<INVTRAN><FITID>R1<DTTRADE>20260601</INVTRAN>
<SECID><TICKER>VTI</TICKER></SECID>
<TOTAL>300.00
</RETOFCAP>"""
    importers.import_file(conn, _write(tmp_path, "r.qfx", _ofx_inv(body)), account="Broker")
    aid = _acct_id(conn, "Broker")
    roc = conn.execute(
        "SELECT action, amount FROM investment_transactions WHERE fitid='R1'"
    ).fetchone()
    assert roc["action"] == "RtrnCap"
    assert roc["amount"] == 300_00                                # positive: cash received
    assert investments.investment_cash(conn, aid) == -5000_00 + 300_00
    holds = {h["symbol"]: h for h in investments.list_holdings(conn, aid)}
    assert holds["VTI"]["quantity"] == "100"                     # shares unchanged
    assert holds["VTI"]["cost_basis"] == 5000_00 - 300_00        # basis reduced, not income


def test_ofx_margin_interest_and_expense_are_cash_out(conn, tmp_path):
    body = """<MARGININTEREST>
<INVTRAN><FITID>M1<DTTRADE>20260201</INVTRAN>
<TOTAL>-25.00
</MARGININTEREST>
<INVEXPENSE>
<INVTRAN><FITID>E1<DTTRADE>20260301</INVTRAN>
<SECID><TICKER>VTI</TICKER></SECID>
<TOTAL>-10.00
</INVEXPENSE>"""
    importers.import_file(conn, _write(tmp_path, "m.qfx", _ofx_inv(body)), account="Broker")
    aid = _acct_id(conn, "Broker")
    rows = {r["fitid"]: r for r in conn.execute(
        "SELECT fitid, action, amount FROM investment_transactions"
    ).fetchall()}
    assert rows["M1"]["action"] == "MargInt" and rows["M1"]["amount"] == -25_00
    assert rows["E1"]["action"] == "MiscExp" and rows["E1"]["amount"] == -10_00
    assert investments.investment_cash(conn, aid) == -25_00 - 10_00  # both cash OUT
    assert investments.list_holdings(conn, aid) == []                # neither creates a holding


def test_ofx_unmapped_action_is_audited_not_silent(conn, tmp_path, caplog):
    body = _OFX_BUY_AAPL + """
<JRNLSEC>
<INVTRAN><FITID>J1<DTTRADE>20260301</INVTRAN>
<SECID><TICKER>AAPL</TICKER></SECID>
<SUBACCTFROM>CASH<SUBACCTTO>MARGIN<UNITS>5
</JRNLSEC>"""
    ofx = _ofx_inv(body)
    with caplog.at_level("WARNING", logger="mammon.importers.ofx"):
        res = importers.import_file(conn, _write(tmp_path, "j.qfx", ofx), account="Broker")
    # the mapped buy still imported; the unmapped JRNLSEC did NOT slip in as a
    # silent cash-in default -- it was dropped AND surfaced.
    assert res.investments == 1
    assert res.unmapped_actions == ["JRNLSEC"]
    assert conn.execute(
        "SELECT 1 FROM investment_transactions WHERE fitid='J1'"
    ).fetchone() is None
    assert "JRNLSEC" in caplog.text
    # the audit helper is pure and callable on raw text
    assert unmapped_investment_actions(ofx) == ["JRNLSEC"]


# ---------------------------------------------------------------------------
# OFX <INVPOSLIST>/<INVPOS> holdings snapshot: capture UNITPRICE into
# price_history (keyed to the position's as-of date, so valuation picks the
# latest price on/before any query date) and reconcile reported share counts /
# prices against holdings computed from transactions -- surfacing discrepancies
# rather than silently overwriting the computed holdings.
# ---------------------------------------------------------------------------
def _ofx_inv_pos(txns: str, poslist: str, seclist: str = "", dtasof: str = "20260131") -> str:
    """A minimal single-account investment statement carrying an <INVPOSLIST>
    holdings snapshot (and optional <SECLIST>) alongside its transactions."""
    return (
        "<OFX>\n"
        "<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>\n"
        f"<DTASOF>{dtasof}\n"
        "<INVACCTFROM><ACCTID>Z1</ACCTID></INVACCTFROM>\n"
        "<INVTRANLIST>\n"
        f"{txns}\n"
        "</INVTRANLIST>\n"
        f"<INVPOSLIST>\n{poslist}\n</INVPOSLIST>\n"
        "</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>\n"
        f"{seclist}\n"
        "</OFX>\n"
    )


_OFX_SECLIST_AAPL = """<SECLISTMSGSRSV1><SECLIST>
<STOCKINFO><SECINFO>
<SECID><UNIQUEID>037833100</UNIQUEID><UNIQUETYPE>CUSIP</SECID>
<SECNAME>Apple Inc<TICKER>AAPL
</SECINFO></STOCKINFO>
</SECLIST></SECLISTMSGSRSV1>"""


def test_ofx_positions_capture_price_and_reconcile_clean(conn, tmp_path):
    from decimal import Decimal
    # The <INVPOS> identifies AAPL ONLY by CUSIP; the <SECLIST> resolves it to the
    # ticker so the snapshot keys by the same symbol the (ticker-based) buy does.
    poslist = """<POSSTOCK>
<INVPOS>
<SECID><UNIQUEID>037833100</UNIQUEID><UNIQUETYPE>CUSIP</SECID>
<HELDINACCT>CASH<POSTYPE>LONG<UNITS>10<UNITPRICE>175.50<MKTVAL>1755.00
<DTPRICEASOF>20260131
</INVPOS>
</POSSTOCK>"""
    ofx = _ofx_inv_pos(_OFX_BUY_AAPL, poslist, _OFX_SECLIST_AAPL)
    res = importers.import_file(conn, _write(tmp_path, "p.qfx", ofx), account="Broker")
    # (a) UNITPRICE landed in price_history keyed to the position's as-of date;
    # valuation (latest price on/before) resolves it for that date and any later one.
    assert investments.latest_price(conn, "AAPL", as_of="2026-01-31") == Decimal("175.50")
    assert investments.latest_price(conn, "AAPL", as_of="2026-03-01") == Decimal("175.50")
    # (b) reported 10 shares == holdings computed from the single buy -> no discrepancy,
    # even though the position named AAPL by CUSIP (SECLIST kept both sides aligned).
    assert res.position_discrepancies == []
    # the parser is pure and callable on raw text
    pos = parse_ofx_positions(ofx)
    assert [(p.symbol, p.date, p.units, p.unitprice) for p in pos] == [
        ("AAPL", "2026-01-31", "10", "175.50")
    ]


def test_ofx_positions_reconcile_surfaces_share_and_price_discrepancies(conn, tmp_path):
    from decimal import Decimal
    # An authoritative quote already on file for the exact (symbol, date): the
    # reported UNITPRICE must be SURFACED as a conflict, never silently overwrite it.
    investments.record_prices(conn, [("AAPL", "2026-01-31", "160.00", "seed")])
    poslist = """<POSSTOCK>
<INVPOS>
<SECID><TICKER>AAPL</TICKER></SECID>
<HELDINACCT>CASH<POSTYPE>LONG<UNITS>12<UNITPRICE>175.00<MKTVAL>2100.00
<DTPRICEASOF>20260131
</INVPOS>
</POSSTOCK>"""
    ofx = _ofx_inv_pos(_OFX_BUY_AAPL, poslist)
    res = importers.import_file(conn, _write(tmp_path, "d.qfx", ofx), account="Broker")
    fields = {(d["field"], d["symbol"]): d for d in res.position_discrepancies}
    # reported 12 shares vs 10 implied by the single buy
    q = fields[("quantity", "AAPL")]
    assert q["computed"] == "10" and q["reported"] == "12"
    # existing 160 quote conflicts with the reported 175 -> surfaced...
    p = fields[("price", "AAPL")]
    assert p["computed"] == "160" and p["reported"] == "175"
    # ...and NOT overwritten (position prices record if-absent only).
    assert investments.latest_price(conn, "AAPL", as_of="2026-01-31") == Decimal("160")
    assert res.summary().endswith("0 err") and "position-discrepancy" in res.summary()


# ---------------------------------------------------------------------------
# JSON (webSlinger)
# ---------------------------------------------------------------------------
JSON_FLAT = """{
 "transactions[0]": {"transactionDate":"1/5/26","payeeName":"Venmo","transactionAmount":"25.00","type":"debit","thirdParty":"John Doe"},
 "transactions[1]": {"transactionDate":"1/10/26","payeeName":"Refund","amount_cents":5000,"type":"credit"}
}"""


def test_json_flat_import_signs(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "v.json", JSON_FLAT), account="Venmo Card")
    assert res.added == 2
    acct = _acct_id(conn, "Venmo Card")
    assert ledger.account_balance(conn, acct) == -25_00 + 50_00


def test_json_list_form(conn, tmp_path):
    text = '[{"date":"2026-01-01","payee":"A","amount":"-10.00"},' \
           '{"date":"2026-01-02","payee":"B","amount":"10.00"}]'
    res = importers.import_file(conn, _write(tmp_path, "l.json", text), account="J")
    assert res.added == 2
    assert ledger.account_balance(conn, _acct_id(conn, "J")) == 0


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
CSV_BANK = """Date,Description,Debit,Credit,Category
01/05/2026,Safeway,25.00,,Groceries
01/10/2026,Paycheck,,200.00,Salary
"""


def test_csv_bank_debit_credit(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "c.csv", CSV_BANK), account="Chase")
    assert res.added == 2
    assert ledger.account_balance(conn, _acct_id(conn, "Chase")) == -25_00 + 200_00


CSV_FIDELITY = """Run Date,Action,Symbol,Quantity,Price ($),Amount ($),Commission ($)
02/01/2026,YOU BOUGHT,AAPL,10,150.00,-1500.00,4.95
"""


def test_csv_investment(conn, tmp_path):
    res = importers.import_file(conn, _write(tmp_path, "f.csv", CSV_FIDELITY), account="Fido")
    assert res.investments == 1
    row = conn.execute(
        "SELECT symbol, quantity, amount, commission FROM investment_transactions"
    ).fetchone()
    assert row["symbol"] == "AAPL" and row["quantity"] == "10"
    assert row["amount"] == -1500_00 and row["commission"] == 495


# ---------------------------------------------------------------------------
# NAV-unit CSV column profiles (401k / 529 / mutual-fund statement exports).
# These carry a fund (often ticker-less -> its NAME is the holding key), a unit
# count, and a per-unit NAV, but NO per-lot cost basis. The profile translates the
# statement's own activity words into canonical actions: a contribution/purchase
# that buys units -> BuyX (adds shares + cost from the dollar amount, cash nets to
# zero), a reinvested dividend -> ReinvDiv, a withdrawal/redemption -> SellX. The
# per-unit NAV rides the txn's price into price_history (source 'txn').
# ---------------------------------------------------------------------------
NETBENEFITS_401K_CSV = """Date,Investment,Transaction Type,Amount,Shares,Share Price
07/15/2026,VANGUARD TARGET 2050,Contribution,250.00,10.000,25.00
07/31/2026,VANGUARD TARGET 2050,Dividend,12.50,0.500,25.00
"""


def test_csv_netbenefits_401k_profile(conn, tmp_path):
    res = importers.import_file(
        conn, _write(tmp_path, "nb.csv", NETBENEFITS_401K_CSV), account="NetBenefits"
    )
    assert res.investments == 2
    aid = _acct_id(conn, "NetBenefits")
    assert _acct_type(conn, "NetBenefits") == "investment"
    # Contribution -> BuyX (share-adding, cash-neutral), NOT the raw "Contribution".
    actions = [
        r["action"] for r in conn.execute(
            "SELECT action FROM investment_transactions WHERE account_id=? ORDER BY date", (aid,)
        ).fetchall()
    ]
    assert actions == ["BuyX", "ReinvDiv"]
    # 10 contributed units + 0.5 reinvested-dividend units, cost = the dollars in.
    h = investments.get_holding(conn, aid, "VANGUARD TARGET 2050")
    assert h["quantity"] == "10.5"
    assert h["cost_basis"] == 250_00 + 12_50
    # The NAV landed in price_history keyed by the fund NAME (ticker-less fund).
    px = conn.execute(
        "SELECT close_price FROM price_history WHERE symbol=? AND date=?",
        ("VANGUARD TARGET 2050", "2026-07-31"),
    ).fetchone()
    assert px["close_price"] == "25"


STATE_529_CSV = """Trade Date,Fund,Transaction Type,Dollar Amount,Units,Unit Price
06/01/2026,MI 529 Growth Portfolio,Contribution,500.00,40.000,12.50
06/15/2026,MI 529 Growth Portfolio,Dividend Reinvestment,20.00,1.600,12.50
09/01/2026,MI 529 Growth Portfolio,Withdrawal,125.00,10.000,12.50
"""


def test_csv_mesp_529_profile(conn, tmp_path):
    res = importers.import_file(
        conn, _write(tmp_path, "state529.csv", STATE_529_CSV), account="State 529"
    )
    assert res.investments == 3
    aid = _acct_id(conn, "State 529")
    actions = [
        r["action"] for r in conn.execute(
            "SELECT action FROM investment_transactions WHERE account_id=? ORDER BY date", (aid,)
        ).fetchall()
    ]
    # Withdrawal that redeems units -> SellX (removes shares, relieves basis).
    assert actions == ["BuyX", "ReinvDiv", "SellX"]
    h = investments.get_holding(conn, aid, "MI 529 Growth Portfolio")
    # 40 + 1.6 - 10 units; average-cost basis relieved on the SellX.
    assert h["quantity"] == "31.6"
    assert h["cost_basis"] == 395_00


TROWE_CSV = """Trade Date,Fund Name,Transaction Description,Amount,Shares,Share Price
05/10/2026,T. Rowe Price Blue Chip Growth,Purchase,1000.00,8.000,125.00
05/20/2026,T. Rowe Price Blue Chip Growth,Reinvest Dividend,50.00,0.400,125.00
"""


def test_csv_trowe_price_profile(conn, tmp_path):
    res = importers.import_file(
        conn, _write(tmp_path, "trp.csv", TROWE_CSV), account="T Rowe Price"
    )
    assert res.investments == 2
    aid = _acct_id(conn, "T Rowe Price")
    h = investments.get_holding(conn, aid, "T. Rowe Price Blue Chip Growth")
    assert h["quantity"] == "8.4"
    assert h["cost_basis"] == 1000_00 + 50_00
    px = conn.execute(
        "SELECT close_price FROM price_history WHERE symbol=?",
        ("T. Rowe Price Blue Chip Growth",),
    ).fetchone()
    assert px["close_price"] == "125"


# Wells Fargo credit-card CSV: DATE/DESCRIPTION/AMOUNT/CHECK #/STATUS, no FITID,
# charges negative and payments positive, two identical same-day charges.
WF_CREDIT_CSV = (
    '"DATE","DESCRIPTION","AMOUNT","CHECK #","STATUS"\n'
    '"08/06/2026","ANON MARKET M ANYTOWN UT","-18.90",,"Posted"\n'
    '"07/31/2026","ANON MOBILE ANON.COM GA","-9.36",,"Posted"\n'
    '"07/31/2026","ANON MOBILE ANON.COM GA","-9.36",,"Posted"\n'
    '"07/22/2026","AUTOMATIC PAYMENT - THANK YOU","1850.94",,"Posted"\n'
)


def test_wells_fargo_credit_csv_import(conn, tmp_path):
    p = _write(tmp_path, "CreditCard.csv", WF_CREDIT_CSV)
    res = importers.import_file(conn, p, account="WF Card", account_type="credit")
    assert res.added == 4                       # both identical -9.36 rows land
    aid = _acct_id(conn, "WF Card")
    # auto-created as a CREDIT account, honoring the caller's default type
    assert conn.execute("SELECT type FROM accounts WHERE id=?", (aid,)).fetchone()[0] == "credit"
    # STATUS "Posted" -> cleared
    assert all(r["cleared"] == 1 for r in conn.execute(
        "SELECT cleared FROM transactions WHERE account_id=?", (aid,)).fetchall())
    # charge negative, payment positive
    assert ledger.account_balance(conn, aid) == -18_90 - 9_36 - 9_36 + 1850_94


def test_csv_reimport_dedups_via_synthetic_fitid(conn, tmp_path):
    # A re-download of an overlapping CSV window must dedup exactly, even with no
    # source FITID, via the importer's stable synthetic id.
    p = _write(tmp_path, "CreditCard.csv", WF_CREDIT_CSV)
    importers.import_file(conn, p, account="WF Card", account_type="credit")
    res2 = importers.import_file(conn, p, account="WF Card", account_type="credit")
    assert res2.added == 0 and res2.duplicates == 4
    n = conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                     (_acct_id(conn, "WF Card"),)).fetchone()["c"]
    assert n == 4                               # nothing double-inserted


# ---------------------------------------------------------------------------
# cross-source fuzzy dedup + imports table
# ---------------------------------------------------------------------------
def test_cross_source_fuzzy_dedup(conn, tmp_path):
    # same purchase seen once via OFX (with fitid) and once via CSV (no fitid)
    importers.import_file(conn, _write(tmp_path, "wf.ofx", OFX_BANK), account="WF")
    csv = "Date,Description,Amount\n01/05/2026,Safeway,-25.00\n"
    res = importers.import_file(conn, _write(tmp_path, "wf.csv", csv), account="WF")
    assert res.added == 0 and res.duplicates == 1
    # only the two original OFX rows remain
    n = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (_acct_id(conn, "WF"),)
    ).fetchone()["c"]
    assert n == 2


def test_import_records_in_memory(conn):
    # the non-file entry: a live source (e.g. a webSlinger run) hands us records
    recs = [
        importers.NormalizedTxn(external_account="Cash", date="2026-01-01",
                                amount_cents=-1500, payee="Lunch", category="Dining"),
        importers.NormalizedTxn(external_account="Cash", date="2026-01-02",
                                amount_cents=5000, payee="Gift"),
    ]
    res = importers.import_records(conn, recs, provider="live", source_format="json")
    assert res.added == 2
    assert ledger.account_balance(conn, _acct_id(conn, "Cash")) == -15_00 + 50_00


def test_imports_table_records_run(conn, tmp_path):
    importers.import_file(conn, _write(tmp_path, "all.qif", QIF_BANK), provider="Quicken")
    row = conn.execute(
        "SELECT provider, source_format, status, added_count FROM imports"
    ).fetchone()
    assert row["provider"] == "Quicken"
    assert row["source_format"] == "qif"
    assert row["status"] == "done"
    assert row["added_count"] == 3


# ---------------------------------------------------------------------------
# loan-payment category split on import (Task 52)
# ---------------------------------------------------------------------------
def _setup_mortgage(conn):
    """A synthetic 300k @ 6% / 360-month mortgage with a $200 escrow, whose level
    payment is 1798.65 P&I + 200.00 escrow = 1998.65."""
    from mammon import loans

    aid = ledger.create_account(conn, "Home Mortgage", "liability",
                                opening_balance=-300_000_00)
    loans.set_loan_params(
        conn, aid, original_principal=300_000_00, term_months=360,
        payment_amount=1998_65, origination_date="2024-01-01", interval="monthly",
        rates=[("2024-02-01", "6.0")], extras=[("Escrow", 200_00, "Taxes + ins")])
    return aid


def _mortgage_payment():
    return importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-02-01",
        amount_cents=1998_65, payee="Mortgage Servicer", fitid="PMT-2024-02")


def test_import_splits_loan_payment_into_principal_interest_escrow(conn):
    aid = _setup_mortgage(conn)
    res = importers.import_records(conn, [_mortgage_payment()], provider="test")
    assert res.added == 1

    tid = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=? AND amount=?",
        (aid, "2024-02-01", 1998_65)).fetchone()["id"]
    # the lumped payment is now a --Split-- with three categorized legs
    assert ledger.has_splits(conn, tid) is True
    legs = ledger.get_splits(conn, tid)
    by_cat = {l["category_label"]: l["amount"] for l in legs}
    # first payment on 300k @ 6%/12: interest 1500.00, escrow 200.00, principal 298.65
    assert by_cat["Interest Exp"] == 1500_00
    assert by_cat["Escrow"] == 200_00
    assert by_cat["Principal"] == 1998_65 - 1500_00 - 200_00      # 298.65
    # the legs reconstitute the payment to the cent
    assert sum(l["amount"] for l in legs) == 1998_65


def test_import_loan_split_is_idempotent_on_reimport(conn):
    aid = _setup_mortgage(conn)
    importers.import_records(conn, [_mortgage_payment()], provider="test")
    # re-import the SAME payment (fresh parse) -> deduped, no second txn/splits
    res2 = importers.import_records(conn, [_mortgage_payment()], provider="test")
    assert res2.added == 0 and res2.duplicates == 1
    n_txn = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (aid,)).fetchone()["c"]
    assert n_txn == 1
    tid = conn.execute(
        "SELECT id FROM transactions WHERE account_id=?", (aid,)).fetchone()["id"]
    n_splits = conn.execute(
        "SELECT COUNT(*) c FROM splits WHERE transaction_id=?", (tid,)).fetchone()["c"]
    assert n_splits == 3      # still exactly the three legs, not duplicated


def test_import_loan_split_leaves_non_loan_and_source_split_untouched(conn):
    _setup_mortgage(conn)
    # (a) a plain payment against a NON-loan account is never auto-split
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Checking", date="2024-02-01", amount_cents=-50_00,
        payee="Store")], provider="test")
    tid = conn.execute(
        "SELECT id FROM transactions WHERE account_id=?", (chk,)).fetchone()["id"]
    assert ledger.has_splits(conn, tid) is False

    # (b) a loan-account payment that ARRIVES with its own split is left as-is
    aid = _acct_id(conn, "Home Mortgage")
    importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-03-01", amount_cents=1998_65,
        payee="Servicer", fitid="SRC-SPLIT",
        splits=[("Interest Exp", 1000_00, None), ("Principal", 998_65, None)])],
        provider="test")
    stid = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=?",
        (aid, "2024-03-01")).fetchone()["id"]
    legs = ledger.get_splits(conn, stid)
    # the source's own two legs survive verbatim -- not replaced by the 3-leg split
    assert [(l["category_label"], l["amount"]) for l in legs] == [
        ("Interest Exp", 1000_00), ("Principal", 998_65)]


def test_import_loan_split_skips_draw_and_undersized_payment(conn):
    aid = _setup_mortgage(conn)
    # a DRAW (increases the debt: negative against the liability) is not a payment
    importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-02-15", amount_cents=-500_00,
        payee="Advance", fitid="DRAW-1")], provider="test")
    draw = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=?",
        (aid, "2024-02-15")).fetchone()["id"]
    assert ledger.has_splits(conn, draw) is False

    # a payment too small to cover the period's interest+escrow (principal <= 0)
    # is left as a plain row rather than force-split into a negative principal
    importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-02-20", amount_cents=100_00,
        payee="Partial", fitid="TINY-1")], provider="test")
    tiny = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=?",
        (aid, "2024-02-20")).fetchone()["id"]
    assert ledger.has_splits(conn, tiny) is False


# ---------------------------------------------------------------------------
# scheduled pre-entries + import matching (Task 55)
# ---------------------------------------------------------------------------
def test_scheduled_pre_entry_created_with_correct_split(conn):
    from mammon import loans_schedule

    aid = _setup_mortgage(conn)
    pid = loans_schedule.create_pending_payment(conn, aid, "2024-02-01")

    row = ledger.get_transaction(conn, pid)
    assert row["scheduled"] == 1 and row["cleared"] == 0
    assert row["amount"] == 1998_65
    # the pending row already carries the full principal/interest/escrow split
    by_cat = {l["category_label"]: l["amount"] for l in ledger.get_splits(conn, pid)}
    assert by_cat == {"Interest Exp": 1500_00, "Escrow": 200_00,
                      "Principal": 298_65}
    assert sum(by_cat.values()) == 1998_65
    # idempotent: a second call for the same due date returns the same row
    assert loans_schedule.create_pending_payment(conn, aid, "2024-02-01") == pid


def test_import_matches_pending_and_merges_no_duplicate(conn):
    from mammon import loans_schedule

    aid = _setup_mortgage(conn)
    pid = loans_schedule.create_pending_payment(conn, aid, "2024-02-01")

    # the real bank payment imports (same amount, same account) -> it posts INTO
    # the pending pre-entry, not as a second row
    res = importers.import_records(conn, [_mortgage_payment()], provider="test")
    assert res.matched == 1 and res.added == 0 and res.duplicates == 0

    n_txn = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (aid,)).fetchone()["c"]
    assert n_txn == 1                      # merged, not duplicated
    row = ledger.get_transaction(conn, pid)
    assert row["scheduled"] == 0 and row["cleared"] == 1   # now posted
    assert row["fitid"] == "PMT-2024-02"
    # split survives and still reconciles to the cent
    legs = ledger.get_splits(conn, pid)
    assert len(legs) == 3 and sum(l["amount"] for l in legs) == 1998_65

    # re-importing the same payment is now an ordinary fitid duplicate (no 3rd row)
    res2 = importers.import_records(conn, [_mortgage_payment()], provider="test")
    assert res2.duplicates == 1 and res2.matched == 0
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (aid,)).fetchone()["c"] == 1


def test_import_matches_pending_within_date_window(conn):
    from mammon import loans_schedule

    aid = _setup_mortgage(conn)
    pid = loans_schedule.create_pending_payment(conn, aid, "2024-02-01")
    # the bank posts it a few days late (2024-02-05) -- still the same payment
    late = importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-02-05",
        amount_cents=1998_65, payee="Servicer", fitid="LATE-1")
    res = importers.import_records(conn, [late], provider="test")
    assert res.matched == 1 and res.added == 0
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (aid,)).fetchone()["c"] == 1
    row = ledger.get_transaction(conn, pid)
    assert row["date"] == "2024-02-05"     # merged row adopts the actual date
    assert sum(l["amount"] for l in ledger.get_splits(conn, pid)) == 1998_65


def test_import_without_pending_still_imports_cleanly(conn):
    aid = _setup_mortgage(conn)
    # no pre-entry exists -> the payment imports as a normal new (Task 52-split) row
    res = importers.import_records(conn, [_mortgage_payment()], provider="test")
    assert res.added == 1 and res.matched == 0
    row = conn.execute(
        "SELECT id, scheduled FROM transactions WHERE account_id=?", (aid,)).fetchone()
    assert row["scheduled"] == 0
    assert ledger.has_splits(conn, row["id"]) is True


def test_ensure_pending_payments_windows_and_is_idempotent(conn):
    from mammon import loans_schedule

    aid = _setup_mortgage(conn)
    # a wide-enough window from origination catches the first due date (2024-02-01)
    ids = loans_schedule.ensure_pending_payments(
        conn, aid, "2024-01-28", lead_days=7)
    assert len(ids) == 1
    n = conn.execute("SELECT COUNT(*) c FROM transactions "
                     "WHERE account_id=? AND scheduled=1", (aid,)).fetchone()["c"]
    assert n == 1
    # calling again over the same window creates no duplicates
    ids2 = loans_schedule.ensure_pending_payments(
        conn, aid, "2024-01-28", lead_days=7)
    assert ids2 == ids
    assert conn.execute("SELECT COUNT(*) c FROM transactions "
                        "WHERE account_id=? AND scheduled=1", (aid,)).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# payment-change detection + auto-fix (Task 56)
# ---------------------------------------------------------------------------
def _split_by_cat(conn, aid, date, amount):
    tid = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=? AND amount=? "
        "AND scheduled=0", (aid, date, amount)).fetchone()["id"]
    return {l["category_label"]: l["amount"] for l in ledger.get_splits(conn, tid)}, tid


def test_detect_payment_change_flags_escrow_increase_no_duplicate(conn):
    from mammon import loans_schedule
    aid = _setup_mortgage(conn)
    loans_schedule.create_pending_payment(conn, aid, "2024-03-01")   # old $1998.65
    before = conn.execute("SELECT COUNT(*) c FROM transactions "
                          "WHERE account_id=?", (aid,)).fetchone()["c"]
    # escrow jumped $50 -> the bank draws $2048.65, not the pre-entered amount
    r = importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-03-01", amount_cents=2048_65,
        payee="Mortgage Servicer", fitid="PMT-2024-03")], provider="test")
    assert r.matched == 1 and r.added == 0
    assert len(r.payment_changes) == 1
    chg = r.payment_changes[0]
    assert chg.expected_amount == 1998_65 and chg.actual_amount == 2048_65
    assert chg.delta == 50_00 and chg.scheduled_date == "2024-03-01"
    # merged into the pre-entry -- no duplicate row was created
    after = conn.execute("SELECT COUNT(*) c FROM transactions "
                         "WHERE account_id=?", (aid,)).fetchone()["c"]
    assert after == before
    # and it reconciles to the cent even before the user confirms the fix
    by_cat, _ = _split_by_cat(conn, aid, "2024-03-01", 2048_65)
    assert sum(by_cat.values()) == 2048_65


def test_apply_payment_change_escrow_resplits_forward_to_the_cent(conn):
    from mammon import loans, loans_schedule
    aid = _setup_mortgage(conn)
    loans_schedule.create_pending_payment(conn, aid, "2024-03-01")
    loans_schedule.create_pending_payment(conn, aid, "2024-04-01")   # future entry
    r = importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-03-01", amount_cents=2048_65,
        payee="Mortgage Servicer", fitid="PMT-2024-03")], provider="test")
    chg = r.payment_changes[0]

    # The authoritative new total is the amount the user actually paid (the $2048.65 the
    # bank drew), NOT a computed number -- exactly what the UI dialog pre-fills.
    fixed = loans_schedule.apply_payment_change(
        conn, aid, chg.scheduled_date, change_type="escrow",
        new_escrow_cents=250_00, new_payment_amount=chg.actual_amount)
    assert len(fixed) == 2                       # merged payment + future entry

    # the posted (now reconciled) payment carries the correct new escrow leg
    by_cat, _ = _split_by_cat(conn, aid, "2024-03-01", 2048_65)
    assert by_cat["Escrow"] == 250_00
    assert sum(by_cat.values()) == 2048_65
    # the future pre-entry adopted the new authoritative total + new escrow
    row = conn.execute("SELECT id, amount FROM transactions WHERE account_id=? "
                       "AND date=? AND scheduled=1", (aid, "2024-04-01")).fetchone()
    assert row["amount"] == 2048_65
    legs = {l["category_label"]: l["amount"] for l in ledger.get_splits(conn, row["id"])}
    assert legs["Escrow"] == 250_00 and sum(legs.values()) == 2048_65
    # loan params now record the new escrow + scheduled payment
    lp = loans.get_loan_params(conn, aid)
    assert lp.extras_total == 250_00 and lp.payment_amount == 2048_65


def test_apply_payment_change_stores_new_total_dated_and_applies_forward(conn):
    """On an escrow change the user can enter the NEW total payment; it is stored as
    first-class DATED data so the PROJECTED schedule uses the new total from the
    effective date forward while earlier periods keep the old total. The entered
    total is authoritative -- it overrides the recomputed old_pi + new escrow."""
    from mammon import loans, loans_schedule
    aid = _setup_mortgage(conn)                 # $200 escrow, $1998.65 total, 6%

    # escrow rises to $300 effective 2025-01-01; the user sets the authoritative new
    # total to $2,150.00 (NOT the recomputed old_pi $1798.65 + $300 = $2098.65).
    loans_schedule.apply_payment_change(
        conn, aid, "2025-01-01", change_type="escrow",
        new_escrow_cents=300_00, new_payment_amount=2150_00)

    lp = loans.get_loan_params(conn, aid)
    # the flat "current" total adopts the entered value...
    assert lp.payment_amount == 2150_00
    # ...and the dated history keeps BOTH the pre-change baseline and the new total
    amounts = {p.effective_date: p.amount for p in lp.payments}
    assert amounts["2025-01-01"] == 2150_00
    assert 1998_65 in amounts.values()          # baseline for earlier periods

    # the projected schedule uses the OLD total before the effective date and the
    # NEW total on/after it (each row still reconstitutes to the cent)
    sched = {r.date: r for r in loans.amortization_schedule(conn, aid)}
    assert sched["2024-12-01"].payment == 1998_65
    assert sched["2024-12-01"].escrow == 200_00
    assert sched["2025-01-01"].payment == 2150_00
    assert sched["2025-01-01"].escrow == 300_00
    for d in ("2025-01-01", "2025-02-01", "2025-03-01"):
        r = sched[d]
        assert r.payment == 2150_00
        assert r.principal + r.interest + r.escrow == r.payment


def test_apply_payment_change_rate_reset_resplits_forward(conn):
    from mammon import loans, loans_schedule
    aid = _setup_mortgage(conn)
    pid = loans_schedule.create_pending_payment(conn, aid, "2024-06-01")
    before = {l["category_label"]: l["amount"] for l in ledger.get_splits(conn, pid)}

    fixed = loans_schedule.apply_payment_change(
        conn, aid, "2024-06-01", change_type="rate", new_annual_rate="7.5")
    assert pid in fixed
    after = {l["category_label"]: l["amount"] for l in ledger.get_splits(conn, pid)}
    # a higher rate moves money from principal into interest, total unchanged
    assert after["Interest Exp"] > before["Interest Exp"]
    assert after["Principal"] < before["Principal"]
    assert sum(after.values()) == 1998_65
    assert loans.get_loan_params(conn, aid).payment_amount == 1998_65


def test_apply_payment_change_never_synthesizes_a_computed_total(conn):
    """(d) Regression guard for the user's correction: apply_payment_change with NO
    authoritative new total must NEVER replace the total payment with a computed
    value (the removed ``old_pi + new_escrow_total`` overwrite). The stored total
    stays put; only the split is re-derived -- interest/principal/escrow are the
    only computed quantities, and the escrow increase comes out of principal."""
    from mammon import loans, loans_schedule
    aid = _setup_mortgage(conn)                 # $200 escrow, $1998.65 total
    before = loans.get_loan_params(conn, aid).payment_amount
    assert before == 1998_65

    # escrow bumps $200 -> $260 effective 2024-05-01, but the user did NOT re-key the
    # total (no new_payment_amount): the total MUST be preserved, not recomputed.
    loans_schedule.apply_payment_change(
        conn, aid, "2024-05-01", change_type="escrow", new_escrow_cents=260_00)

    lp = loans.get_loan_params(conn, aid)
    assert lp.payment_amount == before               # total UNCHANGED...
    assert lp.payment_amount != 1798_65 + 260_00     # ...the computed value never wins
    assert lp.payments == []                         # no dated total row was fabricated
    assert lp.extras_total == 260_00                 # only the escrow moved

    # the split reconstitutes the (unchanged) total, escrow now $260, principal
    # ABSORBING the increase -- principal = total - interest - escrow (computed)
    split = loans.payment_split(conn, aid, "2024-05-01", before)
    assert split.escrow == 260_00
    assert split.principal + split.interest + split.escrow == before
    assert split.principal == before - split.interest - 260_00


def test_exact_amount_match_is_not_flagged_as_change(conn):
    from mammon import loans_schedule
    aid = _setup_mortgage(conn)
    loans_schedule.create_pending_payment(conn, aid, "2024-03-01")
    r = importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-03-02", amount_cents=1998_65,
        payee="Mortgage Servicer", fitid="PMT-2024-03")], provider="test")
    assert r.matched == 1 and r.payment_changes == []


def test_dated_escrow_change_corrects_only_forward_posted_splits(conn):
    """A dated escrow change re-splits ALREADY-POSTED payments only from its
    effective date forward: earlier posted rows keep their old escrow untouched,
    on/after rows are corrected, and (because the change is stored as first-class
    dated data) a freshly-derived split reproduces the right amount for any date
    without consulting the posted rows. The running balance stays consistent."""
    from mammon import loans, loans_schedule
    aid = _setup_mortgage(conn)                 # $200 escrow, $1998.65 payment
    dates = ["2024-02-01", "2024-03-01", "2024-04-01", "2024-05-01", "2024-06-01"]
    for d in dates:
        importers.import_records(conn, [importers.NormalizedTxn(
            external_account="Home Mortgage", date=d, amount_cents=1998_65,
            payee="Mortgage Servicer", fitid=f"PMT-{d}")], provider="test")
    # every posted payment splits with the original $200 escrow, to the cent
    for d in dates:
        by_cat, _ = _split_by_cat(conn, aid, d, 1998_65)
        assert by_cat["Escrow"] == 200_00 and sum(by_cat.values()) == 1998_65

    # escrow rises to $250 effective 2024-04-01
    fixed = loans_schedule.apply_payment_change(
        conn, aid, "2024-04-01", change_type="escrow", new_escrow_cents=250_00)

    # ONLY the payments on/after the effective date were re-split
    fixed_dates = {conn.execute("SELECT date FROM transactions WHERE id=?",
                                (i,)).fetchone()["date"] for i in fixed}
    assert fixed_dates == {"2024-04-01", "2024-05-01", "2024-06-01"}

    # payments BEFORE the change keep $200 escrow (untouched)
    for d in ("2024-02-01", "2024-03-01"):
        by_cat, _ = _split_by_cat(conn, aid, d, 1998_65)
        assert by_cat["Escrow"] == 200_00 and sum(by_cat.values()) == 1998_65
    # payments ON/AFTER get $250, still reconstituting the actual posted amount
    for d in ("2024-04-01", "2024-05-01", "2024-06-01"):
        by_cat, _ = _split_by_cat(conn, aid, d, 1998_65)
        assert by_cat["Escrow"] == 250_00 and sum(by_cat.values()) == 1998_65

    # DETERMINISM: the change persists as first-class dated data, so a fresh split
    # reproduces the right escrow for any date (no posted row is consulted)
    assert loans.payment_split(conn, aid, "2024-03-01", 1998_65).escrow == 200_00
    assert loans.payment_split(conn, aid, "2024-04-01", 1998_65).escrow == 250_00
    lp = loans.get_loan_params(conn, aid)
    # No authoritative new total was supplied and every actual payment stayed
    # $1998.65 (the user did not re-key his payment after the escrow bump), so the TOTAL
    # is preserved -- the extra $50 escrow comes out of principal, it is NOT added
    # back onto a recomputed total. The total is never synthesized.
    assert lp.extras_total == 250_00 and lp.payment_amount == 1998_65

    # the running balance stays consistent: the schedule declines monotonically
    # and every row still reconstitutes to the cent
    sched = loans.amortization_schedule(conn, aid)
    balances = [300_000_00] + [r.balance for r in sched]
    assert all(b2 < b1 for b1, b2 in zip(balances, balances[1:]))
    for r in sched:
        assert r.principal + r.interest + r.escrow == r.payment
# ---------------------------------------------------------------------------
# Transfer import PATHS: dual-account QIF vs single-account import/download.
#
# Requirement: a QIF that carries BOTH accounts of a transfer imports both legs
# and LINKS them (no fabricated mirror); a SINGLE-account import/download creates
# the mirror in the counterparty and goes through the review list, where a match
# to an existing register leg prevents double-entry. A single-account QIF is
# treated exactly like a download.
# ---------------------------------------------------------------------------
QIF_CHECKING_ONLY = """!Account
NChecking
TBank
^
!Type:Bank
D01/05'26
T-25.00
PSafeway
LGroceries
^
D01/15'26
PTransfer to savings
L[Savings]
T-40.00
^
"""

QIF_SAVINGS_ONLY = """!Account
NSavings
TBank
^
!Type:Bank
D01/15'26
PTransfer from checking
L[Checking]
T40.00
^
"""


def test_distinct_account_names_discriminates_single_vs_multi():
    """The routing discriminator: a single-account file names ONE owning account
    (transfer counterparties are not counted); a dual-account QIF names both."""
    from mammon.importers.qif import parse_qif
    assert importers.distinct_account_names(parse_qif(QIF_CHECKING_ONLY)) == {"Checking"}
    assert importers.distinct_account_names(parse_qif(QIF_BANK)) == {"Checking", "Savings"}


def test_both_accounts_in_qif_link_without_mirror(conn, tmp_path):
    """PATH 1 -- a QIF carrying BOTH legs collapses to ONE linked pair. No mirror
    is fabricated: exactly two transfer rows, cross-linked, no orphan one-sided
    leg (transfer_pair_id NULL) left behind."""
    importers.import_file(conn, _write(tmp_path, "all.qif", QIF_BANK))
    rows = conn.execute(
        "SELECT id, amount, transfer_pair_id "
        "FROM transactions WHERE transfer_account_id IS NOT NULL ORDER BY amount"
    ).fetchall()
    assert len(rows) == 2
    neg, pos = rows
    assert neg["transfer_pair_id"] == pos["id"]
    assert pos["transfer_pair_id"] == neg["id"]
    orphans = conn.execute(
        "SELECT COUNT(*) c FROM transactions "
        "WHERE transfer_account_id IS NOT NULL AND transfer_pair_id IS NULL"
    ).fetchone()["c"]
    assert orphans == 0


def test_single_account_qif_creates_counterparty_mirror(conn, tmp_path):
    """PATH 2 -- a SINGLE-account QIF whose only transfer leg points at a not-yet-
    existing counterparty: import_single_account (same path as a download) creates
    the counterparty account AND the reciprocal mirror leg, cross-linked."""
    summary = importers.import_single_account(
        conn, path=_write(tmp_path, "checking.qif", QIF_CHECKING_ONLY), account="Checking")
    assert summary == {"added": 2, "matched": 0}   # grocery + transfer; nothing matched

    checking = _acct_id(conn, "Checking")
    savings = _acct_id(conn, "Savings")            # auto-created counterparty
    rows = conn.execute(
        "SELECT id, account_id, amount, transfer_account_id, transfer_pair_id "
        "FROM transactions WHERE transfer_account_id IS NOT NULL ORDER BY amount"
    ).fetchall()
    assert len(rows) == 2
    neg, pos = rows
    assert neg["account_id"] == checking and neg["amount"] == -40_00
    assert pos["account_id"] == savings and pos["amount"] == 40_00
    assert neg["transfer_account_id"] == savings and pos["transfer_account_id"] == checking
    assert neg["transfer_pair_id"] == pos["id"] and pos["transfer_pair_id"] == neg["id"]
    # the plain grocery row imported too, with its category
    assert ledger.account_balance(conn, checking) == -25_00 - 40_00
    assert ledger.account_balance(conn, savings) == 40_00


def test_single_account_qif_matches_existing_counterparty_no_double_entry(conn, tmp_path):
    """PATH 2 double-entry guard -- import Checking first (creating the Savings
    mirror), THEN import Savings' own file. Its 'transfer from Checking' leg must
    MATCH the mirror already in Savings' register, not add a second leg."""
    importers.import_single_account(
        conn, path=_write(tmp_path, "chk.qif", QIF_CHECKING_ONLY), account="Checking")
    savings = _acct_id(conn, "Savings")
    before = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (savings,)).fetchone()["c"]

    summary = importers.import_single_account(
        conn, path=_write(tmp_path, "sav.qif", QIF_SAVINGS_ONLY), account="Savings")
    assert summary == {"added": 0, "matched": 1}

    after = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (savings,)).fetchone()["c"]
    assert after == before                     # matched the mirror; no second leg
    assert ledger.account_balance(conn, savings) == 40_00


def test_single_account_review_finalize_false_returns_entries(conn, tmp_path):
    """finalize=False hands the classified review list back to a UI and writes
    NOTHING to the register yet -- the download-style two-step."""
    entries = importers.import_single_account(
        conn, path=_write(tmp_path, "c.qif", QIF_CHECKING_ONLY),
        account="Checking", finalize=False)
    assert len(entries) == 2
    assert [e.label for e in entries].count("NEW") == 2
    assert conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 0


# ---- broker QIF exports ------------------------------------------------------
_INVST_BODY = ("D01/15/2026\nNBuy\nYApple Inc\nQ10\nI150.00\nT1500.00\n^\n")


def test_qif_accepts_the_unabbreviated_invest_header(tmp_path):
    """Quicken writes !Type:Invst, but brokers exporting "Quicken format" also
    emit !Type:Invest -- which shares no substring with "invst" (invEst vs
    invst). It fell through to the metadata branch, so the whole file parsed to
    ZERO transactions with no error at all."""
    from mammon.importers.qif import parse_qif

    for header in ("!Type:Invst", "!Type:Invest", "!Type:Invst\n", "!Type:INVEST"):
        records = parse_qif(header.rstrip("\n") + "\n" + _INVST_BODY,
                            default_account="Brokerage")
        assert len(records) == 1, f"{header!r} parsed to nothing"
        assert records[0].action == "Buy"
        assert records[0].symbol == "Apple Inc"
        assert records[0].account_type == "investment"


def test_security_master_qif_without_an_account_block_needs_the_fallback(tmp_path, conn):
    """A broker's QIF routinely carries a !Type:Security master, which routes the
    file down the BULK path -- but such a file often names no account of its own.
    Without an account fallback every row parsed to an account of None and the
    import added nothing while reporting success."""
    from mammon import importers, ledger

    path = tmp_path / "broker.qif"
    path.write_text("!Type:Security\nNApple Inc\nSAAPL\nTStock\n^\n"
                    "!Type:Invst\n" + _INVST_BODY, encoding="utf-8")
    # The securities master is what sends it down the bulk route.
    assert importers.multi_account_file(str(path)) is True

    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    res = importers.import_file(conn, str(path), account="Brokerage",
                                account_type="investment")
    assert res.added == 1
    rows = conn.execute(
        "SELECT account_id, symbol FROM investment_transactions").fetchall()
    assert [tuple(r) for r in rows] == [(acct, "Apple Inc")]

    # Without the fallback the same file silently imports nothing.
    from mammon import db as _db
    other = _db.init_db(tmp_path / "bare.db")
    ledger.create_account(other, "Brokerage", "investment", opening_balance=0)
    bare = importers.import_file(other, str(path))
    assert bare.added == 0, "regression guard: this is the bug being fixed"
    other.close()


def test_broker_qif_variants_all_import(tmp_path, conn):
    """Line endings, a BOM, ISO dates, Quicken's apostrophe year, an !Account
    block and a cash-only investment action are all shapes a broker export
    actually takes."""
    from mammon.importers.qif import parse_qif

    variants = {
        "crlf+bom": "\ufeff!Type:Invst\r\n" + _INVST_BODY.replace("\n", "\r\n"),
        "iso dates": "!Type:Invst\nD2026-01-15\nNBuy\nYApple Inc\nQ10\nI150.00\nT1500.00\n^\n",
        "apostrophe year": "!Type:Invst\nD1/15' 26\nNBuy\nYApple Inc\nQ10\nI150.00\nT1500.00\n^\n",
        "account block": "!Account\nNBrokerage\nTInvst\n^\n!Type:Invst\n" + _INVST_BODY,
        "cash action": "!Type:Invst\nD01/15/2026\nNXIn\nT500.00\n^\n",
    }
    for label, body in variants.items():
        records = parse_qif(body, default_account="Brokerage")
        assert len(records) == 1, f"{label} parsed to nothing"
        assert records[0].date.startswith("2026-01-15"), label


# ---- one plan, two export formats -------------------------------------------
_PLAN_QIF = ("!Type:Invst\n"
             "D01/07/2026\nNShrsOut\nYTARGET 2030 FUND(TDLB)\nI30.33\nQ0.125\nT3.78\nMFees\n^\n"
             "D01/07/2026\nNShrsOut\nYS&P 500 EQUITY INDEX(TDLJ)\nI12.72\nQ0.062\nT0.76\n^\n")

_PLAN_CSV = ("\n"
             "Plan name:,EXAMPLE 401(K) PLAN\n"
             "Date Range,10/09/2025 - 08/29/2026,,,,\n"
             "\n\n"
             "Date,Investment,Transaction Type,Amount,Shares/Unit\n"
             '01/07/2026,TARGET 2030 FUND,RECORDKEEPING FEE,"-3.78","-0.125"\n'
             '01/07/2026,S&amp;P 500 EQUITY INDEX,ADMINISTRATIVE FEES,"-0.76","-0.062"\n'
             '01/07/2026,TARGET 2030 FUND,Change in Market Value,"2.08","0.000"\n')


def test_security_name_repair_fixes_encoding_but_never_identity():
    """The line here matters. A CSV writing ``S&amp;P 500 EQUITY INDEX`` has a
    broken escape, not a different fund -- repairing it is reading the file
    correctly. Deciding that ``TARGET 2030 FUND(TDLB)`` IS ``TARGET 2030 FUND``
    is a judgement about security identity, which no rule makes reliably:
    investment firms rename funds often, and a parenthetical can equally mark a
    different share class. That decision belongs to the user, at review."""
    from mammon.importers.record import normalize_security_name as norm

    # Encoding defects: repaired.
    assert norm("S&amp;P 500 EQUITY INDEX") == "S&P 500 EQUITY INDEX"
    assert norm("  spaced   out  ") == "spaced out"
    assert norm(None) == ""

    # Identity: left exactly as the file stated it.
    assert norm("TARGET 2030 FUND(TDLB)") == "TARGET 2030 FUND(TDLB)"
    assert norm("VANGUARD 500 (ADMIRAL)") == "VANGUARD 500 (ADMIRAL)"


def test_the_two_export_formats_name_securities_differently(tmp_path, conn):
    """A standing record of WHY security matching has to be a review step: the
    same plan, the same day, exports the same fund under two different names.
    Nothing here resolves that -- the import records what the file said, and the
    user decides whether it is a new security or an existing one."""
    from mammon import importers, ledger

    def securities_from(name, body):
        c = ledger.create_account(conn, name, "investment", opening_balance=0)
        path = tmp_path / f"{name}.{'qif' if body is _PLAN_QIF else 'csv'}"
        path.write_text(body, encoding="utf-8")
        importers.import_single_account(
            conn, account=name, account_type="investment",
            path=str(path), finalize=True)
        return sorted(r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM investment_transactions "
            "WHERE account_id=? AND symbol IS NOT NULL", (c,)))

    qif_names = securities_from("qif-side", _PLAN_QIF)
    csv_names = securities_from("csv-side", _PLAN_CSV)
    # The QIF appends the ticker; the CSV does not. Both are recorded verbatim.
    assert qif_names == ["S&P 500 EQUITY INDEX(TDLJ)", "TARGET 2030 FUND(TDLB)"]
    assert csv_names == ["S&P 500 EQUITY INDEX", "TARGET 2030 FUND"]
    assert qif_names != csv_names, "if these ever agree, re-read the docstring"


def test_plan_fee_rows_are_mapped_and_nothing_is_silently_dropped(tmp_path, conn):
    """Plan fees are MAPPED to ShrsOut -- a guess about what this institution
    means by "RECORDKEEPING FEE", of the kind Quicken gets wrong often enough
    that correcting it is routine. The guess is survivable only because the row
    is visible and editable before it is accepted.

    Nothing is filtered on activity text. "Change in Market Value" almost
    certainly is not a transaction, but dropping it decides that for the user
    silently and leaves nothing to correct or delete."""
    from mammon import importers, ledger

    acct = ledger.create_account(conn, "401k", "investment", opening_balance=0)
    path = tmp_path / "history.csv"
    path.write_text(_PLAN_CSV, encoding="utf-8")
    summary = importers.import_single_account(
        conn, account="401k", account_type="investment",
        path=str(path), finalize=True)

    assert summary["added"] == 3, "every dated row must reach the user"
    actions = dict(conn.execute(
        "SELECT action, COUNT(*) FROM investment_transactions "
        "WHERE account_id=? GROUP BY action", (acct,)).fetchall())
    assert actions["ShrsOut"] == 2                    # both fee rows mapped
    # The valuation row arrives under its own name, for the user to deal with.
    assert "Change in Market Value" in actions


# ---- investment-statement cash (OFX <INVBANKTRAN>) ---------------------------
_INV_QFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
<INVTRANLIST>
<INCOME>
<INVTRAN><FITID>DIV1</FITID><DTTRADE>20260107202000.000[-5:EST]</DTTRADE>
<MEMO>ALTY(US37954Y8066) CASH DIVIDEND</MEMO></INVTRAN>
<SECID><UNIQUEID>37954Y806</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>
<INCOMETYPE>DIV</INCOMETYPE><TOTAL>97.5</TOTAL>
</INCOME>
<INVBANKTRAN>
<STMTTRN><TRNTYPE>INT</TRNTYPE><DTPOSTED>20260106202000.000[-5:EST]</DTPOSTED>
<TRNAMT>4.97</TRNAMT><FITID>INT1</FITID><MEMO>USD CREDIT INT FOR DEC-2025</MEMO>
</STMTTRN><SUBACCTFUND>CASH</SUBACCTFUND>
</INVBANKTRAN>
<INVBANKTRAN>
<STMTTRN><TRNTYPE>FEE</TRNTYPE><DTPOSTED>20260105202000.000[-5:EST]</DTPOSTED>
<TRNAMT>-4.50</TRNAMT><FITID>FEE1</FITID><MEMO>MONTHLY DATA FEE</MEMO>
</STMTTRN><SUBACCTFUND>CASH</SUBACCTFUND>
</INVBANKTRAN>
</INVTRANLIST>
<SECLIST><STOCKINFO><SECINFO>
<SECID><UNIQUEID>37954Y806</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>
<SECNAME>ALTY GLOBAL X ALTERNATIVE INCOME</SECNAME><TICKER>ALTY</TICKER>
</SECINFO></STOCKINFO></SECLIST>
</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>
</OFX>
"""

_BANK_QFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>INT</TRNTYPE><DTPOSTED>20260106</DTPOSTED><TRNAMT>1.25</TRNAMT>
<FITID>B1</FITID><NAME>INTEREST PAID</NAME></STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def test_investment_statement_cash_becomes_investment_rows():
    """An <INVBANKTRAN> is the brokerage's own cash activity inside an investment
    account -- interest credited, account fees. Routing it to the cash table (its
    literal OFX shape) made the destination depend on the FILE FORMAT: the same
    interest landed in investment_transactions from a broker's CSV and in
    transactions from its QFX, so the two never deduped and importing both
    duplicated every interest and fee row."""
    from mammon.importers import ofx

    recs = ofx.parse_ofx(_INV_QFX, default_account="IB")
    assert len(recs) == 3
    by_action = {r.action: r for r in recs}
    assert set(by_action) == {"Div", "IntInc", "MiscExp"}
    # ALL of them are investment rows -- none fall through to cash.
    for r in recs:
        assert r.account_type == "investment", r.action
    assert by_action["IntInc"].amount_cents == 497
    assert by_action["MiscExp"].amount_cents == -450
    assert by_action["IntInc"].fitid == "INT1"
    # The dividend still resolves its CUSIP to a ticker through <SECLIST>.
    assert by_action["Div"].symbol == "ALTY"


def test_plain_bank_statement_still_yields_cash_rows():
    """The converse: an ordinary bank/CC STMTTRN is unaffected -- only an
    STMTTRN nested inside <INVBANKTRAN> reroutes."""
    from mammon.importers import ofx

    recs = ofx.parse_ofx(_BANK_QFX, default_account="Checking")
    assert len(recs) == 1
    assert recs[0].account_type != "investment"
    assert recs[0].action == ""
    assert recs[0].payee == "INTEREST PAID"
    assert recs[0].amount_cents == 125


def test_invbank_transfer_direction_follows_the_amount():
    """The OFX spec's XFER carries either direction; the sign decides."""
    from mammon.importers import ofx

    tpl = _INV_QFX.replace("<TRNTYPE>INT</TRNTYPE>", "<TRNTYPE>XFER</TRNTYPE>")
    recs = {r.fitid: r for r in ofx.parse_ofx(tpl, default_account="IB")}
    assert recs["INT1"].action == "XIn"            # +4.97
    out = ofx.parse_ofx(tpl.replace("<TRNAMT>4.97</TRNAMT>",
                                    "<TRNAMT>-4.97</TRNAMT>"), default_account="IB")
    assert {r.fitid: r for r in out}["INT1"].action == "XOut"


def test_unmapped_trntype_keeps_its_raw_text_for_review():
    """Only the unambiguous OFX enumeration values are translated; anything else
    reaches review as-is for the user (and the learned action tree) to resolve --
    the importer does not invent a meaning for it."""
    from mammon.importers import ofx

    odd = _INV_QFX.replace("<TRNTYPE>INT</TRNTYPE>", "<TRNTYPE>OTHER</TRNTYPE>")
    recs = {r.fitid: r for r in ofx.parse_ofx(odd, default_account="IB")}
    assert recs["INT1"].action == "OTHER"
    assert recs["INT1"].account_type == "investment"


def test_ofx_split_keeps_an_odd_ratio_exact(tmp_path):
    """OFX states a split as NUMERATOR/DENOMINATOR. A 4:3 has no exact decimal
    form, so the pair is carried whole rather than pre-divided -- 300 shares
    become 400, not 399.99999999999999."""
    from mammon import investments
    from mammon.importers import ofx

    text = """<OFX><INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
    <INVACCTFROM><ACCTID>Z1</ACCTID></INVACCTFROM>
    <INVTRANLIST>
    <SPLIT><INVTRAN><FITID>S1</FITID><DTTRADE>20260421</DTTRADE></INVTRAN>
    <SECID><UNIQUEID>US0000000001</UNIQUEID></SECID>
    <SUBACCTSEC>CASH</SUBACCTSEC><OLDUNITS>300</OLDUNITS><NEWUNITS>400</NEWUNITS>
    <NUMERATOR>4</NUMERATOR><DENOMINATOR>3</DENOMINATOR></SPLIT>
    </INVTRANLIST></INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1></OFX>"""
    rows = [r for r in ofx.parse_ofx(text, default_account="Z1")
            if r.action == "StkSplit"]
    assert len(rows) == 1
    assert (rows[0].split_num, rows[0].split_den) == (4, 3)

    conn = db.init_db(tmp_path / "ofx.db")
    acct = ledger.create_account(conn, "Z1", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2026-01-05", "Buy", symbol="ABC",
                                  quantity="300", price="40", amount=-12_000_00)
    r = rows[0]
    r.symbol = "ABC"
    investments.record_investment(
        conn, acct, r.date, r.action, symbol=r.symbol,
        quantity=r.quantity or None,
        split_num=r.split_num, split_den=r.split_den)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "ABC")["quantity"] == "400"
    conn.close()
