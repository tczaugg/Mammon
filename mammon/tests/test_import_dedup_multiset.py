"""Count-aware multiset import dedup.

the user legitimately makes several identical purchases on one day (same date, amount,
payee, memo), and a single import always carries ALL of that day's purchases. So
dedup must be:

* NEVER within a batch -- N identical incoming rows all land;
* import-vs-register only, by COUNT -- for one identity group, if the register
  already holds R matching rows and the import carries I, insert max(0, I - R).

These tests pin the counts for BOTH engines -- the direct/bulk path
(:func:`mammon.importers.import_records`) and the review-download path
(:func:`mammon.import_review.import_records_via_review`) -- on a synthetic DB and
on a COPY of a real ``data/mammon_2026.db``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from mammon import db, importers, import_review, ledger
from mammon.importers.record import identity_key
from mammon.tests import fresh_db

# Acceptance tests run against a real ledger, and ONLY when one is named
# explicitly via $MAMMON_ACCEPTANCE_DB. They deliberately do NOT fall back to
# probing for data/mammon.db: an unrelated database that merely happened to
# sit at that path made these run against the wrong ledger and fail with
# confusing AttributeErrors, and any test that opens a real ledger by
# accident is one migration away from modifying it.
REAL_DB = Path(os.environ.get("MAMMON_ACCEPTANCE_DB") or "__acceptance_db_not_configured__")

# A future date + odd amount guarantees an EMPTY match window in the real DB
# (no real row within +/-3 days of it), so the real-account tests start clean and
# never trip the QIF-migration cutover watermark (which only covers dates <= the
# newest migrated date).
PROBE_DATE = "2030-06-15"
PROBE_AMT = -4242            # -$42.42
PROBE_PAYEE = "ZZZ_DEDUP_PROBE"
PROBE_MEMO = "multiset probe"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def real_conn(tmp_path):
    """A private, writable COPY of the real database (never the original)."""
    if not REAL_DB.exists():
        pytest.skip(f"real db not found at {REAL_DB}")
    copy = tmp_path / "mammon_copy.db"
    shutil.copy2(REAL_DB, copy)
    c = db.connect(copy)
    yield c
    c.close()


def _probe(n, *, amount=PROBE_AMT, date=PROBE_DATE, payee=PROBE_PAYEE,
           memo=PROBE_MEMO, account="Anytown CU Ck"):
    """`n` identical plain records (same account/date/amount/payee/memo)."""
    return [
        importers.NormalizedTxn(
            external_account=account, date=date, amount_cents=amount,
            payee=payee, memo=memo,
        )
        for _ in range(n)
    ]


def _count(conn, account_id, *, amount=PROBE_AMT, date=PROBE_DATE, payee=PROBE_PAYEE):
    return conn.execute(
        "SELECT COUNT(*) c FROM transactions "
        "WHERE account_id=? AND date=? AND amount=? AND payee=?",
        (account_id, date, amount, payee),
    ).fetchone()["c"]


def _aid(conn, name):
    return conn.execute(
        "SELECT id FROM accounts WHERE name=?", (name,)).fetchone()["id"]


# ---------------------------------------------------------------------------
# the identity key itself
# ---------------------------------------------------------------------------
def test_identity_key_ignores_fitid_and_normalizes():
    # Two same-day identical purchases with DIFFERENT bank ids share one key...
    a = identity_key(2, "2026-08-27", -500, "Coffee #12", "  latte  ")
    b = identity_key(2, "2026-08-27", -500, "coffee #999", "LATTE")
    assert a == b
    # ...but a different amount / payee / memo / date / account does NOT.
    assert identity_key(2, "2026-08-27", -500, "Coffee", "x") != \
        identity_key(2, "2026-08-27", -501, "Coffee", "x")
    assert identity_key(2, "2026-08-27", -500, "Tea", "x") != \
        identity_key(2, "2026-08-27", -500, "Coffee", "x")
    assert identity_key(3, "2026-08-27", -500, "Coffee", "x") != \
        identity_key(2, "2026-08-27", -500, "Coffee", "x")


# ---------------------------------------------------------------------------
# engine A: mammon.importers.import_records (direct / bulk)
# ---------------------------------------------------------------------------
def _import_A(conn, records):
    return importers.import_records(conn, records, provider="test")


def test_A_batch_of_identical_all_land(conn):
    # (1) two identical same-day rows into an account lacking them -> BOTH appear.
    res = _import_A(conn, _probe(2))
    assert res.added == 2 and res.duplicates == 0
    assert _count(conn, _aid(conn, "Anytown CU Ck")) == 2


def test_A_reimport_same_statement_adds_zero(conn):
    _import_A(conn, _probe(2))
    res2 = _import_A(conn, _probe(2))       # (2) re-import -> 0 added
    assert res2.added == 0 and res2.duplicates == 2
    assert _count(conn, _aid(conn, "Anytown CU Ck")) == 2


def test_A_register_has_one_import_two_adds_one(conn):
    _import_A(conn, _probe(1))              # register now holds 1
    res = _import_A(conn, _probe(2))        # (3) I=2, R=1 -> +1, ends at 2
    assert res.added == 1 and res.duplicates == 1
    assert _count(conn, _aid(conn, "Anytown CU Ck")) == 2


def test_A_register_has_two_import_three_adds_one(conn):
    _import_A(conn, _probe(2))              # register now holds 2
    res = _import_A(conn, _probe(3))        # (4) I=3, R=2 -> +1, ends at 3
    assert res.added == 1 and res.duplicates == 2
    assert _count(conn, _aid(conn, "Anytown CU Ck")) == 3


def test_A_non_identical_same_day_never_merged(conn):
    # (5) same day, different amounts -> two distinct groups, both land.
    recs = _probe(1) + _probe(1, amount=-999)
    res = _import_A(conn, recs)
    assert res.added == 2 and res.duplicates == 0
    aid = _aid(conn, "Anytown CU Ck")
    assert _count(conn, aid) == 1
    assert _count(conn, aid, amount=-999) == 1


# ---------------------------------------------------------------------------
# engine B: mammon.import_review.import_records_via_review (review-download)
# ---------------------------------------------------------------------------
def _acct_B(conn):
    return ledger.create_account(conn, "Anytown CU Ck", "checking")


def _import_B(conn, account_id, records):
    return import_review.import_records_via_review(conn, account_id, records)


def test_B_batch_of_identical_all_land(conn):
    aid = _acct_B(conn)
    res = _import_B(conn, aid, _probe(2))
    assert res == {"added": 2, "matched": 0}
    assert _count(conn, aid) == 2


def test_B_reimport_same_statement_adds_zero(conn):
    aid = _acct_B(conn)
    _import_B(conn, aid, _probe(2))
    res2 = _import_B(conn, aid, _probe(2))
    assert res2 == {"added": 0, "matched": 2}
    assert _count(conn, aid) == 2


def test_B_register_has_one_import_two_adds_one(conn):
    aid = _acct_B(conn)
    _import_B(conn, aid, _probe(1))
    res = _import_B(conn, aid, _probe(2))
    assert res == {"added": 1, "matched": 1}
    assert _count(conn, aid) == 2


def test_B_register_has_two_import_three_adds_one(conn):
    aid = _acct_B(conn)
    _import_B(conn, aid, _probe(2))
    res = _import_B(conn, aid, _probe(3))
    assert res == {"added": 1, "matched": 2}
    assert _count(conn, aid) == 3


def test_B_non_identical_same_day_never_merged(conn):
    aid = _acct_B(conn)
    res = _import_B(conn, aid, _probe(1) + _probe(1, amount=-999))
    assert res == {"added": 2, "matched": 0}
    assert _count(conn, aid) == 1 and _count(conn, aid, amount=-999) == 1


def test_B_classification_labels_are_count_aware(conn):
    # The review list the UI renders must itself be count-aware: with 1 already in
    # the register, exactly one of two identical incoming rows is NEW.
    aid = _acct_B(conn)
    _import_B(conn, aid, _probe(1))
    entries = import_review.build_review_from_records(conn, aid, _probe(2))
    labels = sorted(e.label for e in entries)
    assert labels == sorted([import_review.LABEL_MATCHING, import_review.LABEL_NEW])


# ---------------------------------------------------------------------------
# real data: a COPY of data/mammon_2026.db (never synthetic-only)
# ---------------------------------------------------------------------------
def test_real_db_engineA_multiset_cases(real_conn):
    aid = _aid(real_conn, "Anytown CU Ck")
    assert _count(real_conn, aid) == 0        # clean start (future date, odd amt)

    # (1) two identical -> both land
    r1 = importers.import_records(real_conn, _probe(2), provider="test")
    assert r1.added == 2
    assert _count(real_conn, aid) == 2

    # (2) re-import same statement -> 0 added
    r2 = importers.import_records(real_conn, _probe(2), provider="test")
    assert r2.added == 0 and r2.duplicates == 2
    assert _count(real_conn, aid) == 2

    # (4) register has 2, import 3 -> +1
    r3 = importers.import_records(real_conn, _probe(3), provider="test")
    assert r3.added == 1
    assert _count(real_conn, aid) == 3


def test_real_db_engineB_multiset_cases(real_conn):
    aid = _aid(real_conn, "Anytown CU Ck")
    assert _count(real_conn, aid) == 0

    # (3) register has 1, import 2 -> +1 (ends at 2)
    assert _import_B(real_conn, aid, _probe(1)) == {"added": 1, "matched": 0}
    assert _import_B(real_conn, aid, _probe(2)) == {"added": 1, "matched": 1}
    assert _count(real_conn, aid) == 2

    # (2) re-import same 2 -> 0 added
    assert _import_B(real_conn, aid, _probe(2)) == {"added": 0, "matched": 2}
    assert _count(real_conn, aid) == 2

    # (5) a genuinely different same-day purchase still lands
    assert _import_B(real_conn, aid, _probe(1, amount=-777)) == {"added": 1, "matched": 0}
    assert _count(real_conn, aid, amount=-777) == 1
