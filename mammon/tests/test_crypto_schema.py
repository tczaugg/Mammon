"""Phase 1 crypto foundation: schema + account-type classification.

Covers the migration idempotency, that SCHEMA_VERSION tracks the migration
list, that the three new crypto_* tables exist with their expected columns, and
that a type='crypto' account round-trips through mammon.crypto and is classified
investment-like via the single-source-of-truth ledger.INVESTMENT_LIKE_TYPES.

Synthetic data only -- no real wallet addresses or PII (this repo is open
source).
"""
from __future__ import annotations

from decimal import Decimal

from mammon import crypto, db, ledger
from mammon.tests import fresh_db


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_schema_version_matches_migration_list():
    assert db.SCHEMA_VERSION == len(db.MIGRATIONS)


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "mammon.db"
    conn = fresh_db(path)
    v1 = conn.execute("PRAGMA user_version").fetchone()[0]
    tables1 = db.table_names(conn)
    conn.close()

    # A second init on the same file must be a no-op: no migration re-runs,
    # version and tables unchanged.
    conn2 = fresh_db(path)
    v2 = conn2.execute("PRAGMA user_version").fetchone()[0]
    tables2 = db.table_names(conn2)

    assert v1 == v2 == db.SCHEMA_VERSION
    assert tables1 == tables2
    assert {"crypto_transactions", "crypto_holdings",
            "crypto_holdings_checkpoints"} <= tables2


def test_crypto_transactions_columns(tmp_path):
    conn = fresh_db(tmp_path / "mammon.db")
    assert _columns(conn, "crypto_transactions") == {
        "id", "account_id", "date", "action", "symbol", "quantity", "price",
        "amount", "basis", "fee_symbol", "fee_quantity", "fee_amount",
        "transfer_account_id", "transfer_pair_id", "swap_group_id", "tx_hash",
        # ``payee`` (migration 61): the on-chain From/To counterparty that IS the
        # row's payee on a coin-native wallet -- no separate "counterparty" concept.
        "memo", "payee", "import_id", "fitid", "created_at",
        # ``time`` (migration 70): the time of day the source stated, which
        # orders a day's events (SRD 5.8j).
        "time",
    }


def test_crypto_holdings_columns(tmp_path):
    conn = fresh_db(tmp_path / "mammon.db")
    assert _columns(conn, "crypto_holdings") == {
        "id", "account_id", "symbol", "name", "quantity", "cost_basis",
    }


def test_crypto_holdings_checkpoints_columns(tmp_path):
    conn = fresh_db(tmp_path / "mammon.db")
    assert _columns(conn, "crypto_holdings_checkpoints") == {
        "account_id", "year", "symbol", "quantity", "cost_basis", "income",
        "realized", "ever_held", "lots",
    }


def test_crypto_account_round_trips_via_module(tmp_path):
    conn = fresh_db(tmp_path / "mammon.db")
    # Synthetic wallet address -- not a real one.
    aid = crypto.create_account(conn, "Cold Wallet",
                                wallet_address="0xABCDEF0000000000000000000000000000000001")

    acct = crypto.get_account(conn, aid)
    assert acct is not None
    assert acct["type"] == crypto.CRYPTO_ACCOUNT_TYPE == "crypto"
    assert acct["asset_class"] == "crypto"
    # The wallet address lives in account_number (the MCP-blanked column), not a
    # new sensitive column.
    assert acct["account_number"] == "0xABCDEF0000000000000000000000000000000001"

    listed = crypto.list_accounts(conn)
    assert [a["id"] for a in listed] == [aid]


def test_crypto_account_is_classified_investment_like(tmp_path):
    conn = fresh_db(tmp_path / "mammon.db")
    crypto_id = crypto.create_account(conn, "Hot Wallet")
    checking_id = ledger.create_account(conn, "Checking", "checking")

    crypto_acct = crypto.get_account(conn, crypto_id)
    checking_acct = ledger.get_account(conn, checking_id)

    # The distinction the task requires: crypto is a DISTINCT type value but is
    # investment-LIKE for grouping/valuation.
    assert "crypto" in ledger.INVESTMENT_LIKE_TYPES
    assert "investment" in ledger.INVESTMENT_LIKE_TYPES
    assert crypto.is_crypto_account(crypto_acct)
    assert not crypto.is_crypto_account(checking_acct)
    assert crypto.is_investment_like(crypto_acct)
    assert not crypto.is_investment_like(checking_acct)


def test_quantity_text_preserves_wei_scale():
    # 18-decimal wei round-trips through TEXT storage exactly.
    wei = Decimal("1.234567890123456789")
    assert Decimal(crypto._qty_text(wei)) == wei
    assert crypto._qty_text(Decimal("0")) == "0"
    # Exponent-free storage, like investments._qty_text.
    assert crypto._qty_text(Decimal("100")) == "100"


def test_quantity_context_survives_wei_drop():
    # The load-bearing precision guard: adding one wei to a large balance
    # survives under the high-precision context but would be dropped under the
    # default 28-significant-digit context.
    import decimal

    big = Decimal("1e11")
    one_wei = Decimal("1e-18")
    with decimal.localcontext(crypto.quantity_context()):
        summed = big + one_wei
        assert summed != big
        assert summed - big == one_wei
