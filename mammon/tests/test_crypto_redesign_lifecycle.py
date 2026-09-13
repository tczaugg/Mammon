"""End-to-end lifecycle test for the crypto redesign (SRD §5.8; design locked in
review 55a2d8a0).

ONE scenario, exercised entirely through the :mod:`mammon.crypto` domain API -- the
SOLE writer of the ``crypto_*`` tables. It creates a coin-native WALLET account,
credits two DIFFERENT tokens (the ShrsIn analogue: a quantity in, no fiat leg), then
debits each with a coin-native fee leg (the ShrsOut analogue), and asserts the
per-token quantities/holdings are correct and DISTINCT -- 'ETH' and 'LINK' never
merge, the fee is coin-denominated (never USD), and the From/To counterparty rides on
the row's Payee. Addresses are synthetic ANON placeholders (never a real address).
"""
from decimal import Decimal

from mammon import crypto, db


def _q(value):
    """Compare quantities as Decimals so '1.985' and '1.9850' are equal."""
    return Decimal(str(value))


def test_wallet_two_token_lifecycle(tmp_path):
    conn = db.init_db(str(tmp_path / "crypto.db"))

    # A crypto-account is a single address / paper wallet holding ANY coins/tokens.
    wallet = crypto.create_account(
        conn, "Cold Wallet", kind=crypto.CRYPTO_KIND_WALLET,
        wallet_address="0xANON000000000000000000000000000000000ME",
    )
    acct = crypto.get_account(conn, wallet)
    assert crypto.is_wallet_account(acct)
    assert not crypto.is_exchange_account(acct)
    assert crypto.account_kind(acct) == crypto.CRYPTO_KIND_WALLET

    sender = "0xANON00000000000000000000000000000000A1"
    recip_eth = "0xANON00000000000000000000000000000000B2"
    recip_link = "0xANON00000000000000000000000000000000C3"

    # Two DIFFERENT tokens arrive (ShrsIn-style: a quantity in, no fiat leg).
    eth_in = crypto.record_wallet_credit(conn, wallet, "2026-01-01", "ETH", "3.0",
                                         payee=sender)
    crypto.record_wallet_credit(conn, wallet, "2026-01-02", "LINK", "100",
                                payee=sender)

    # Coin-out with a coin-native fee leg, for each token:
    #  - the ETH send pays its gas IN ETH  (same-coin fee)
    #  - the LINK send pays its gas IN ETH (cross-token fee -- LINK is untouched by it)
    crypto.record_wallet_debit(conn, wallet, "2026-01-03", "ETH", "1.0",
                               payee=recip_eth, fee_symbol="ETH", fee_quantity="0.01")
    link_out = crypto.record_wallet_debit(conn, wallet, "2026-01-04", "LINK", "40",
                                          payee=recip_link, fee_symbol="ETH",
                                          fee_quantity="0.005")

    # Per-token quantities are correct AND distinct.
    holdings = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    assert set(holdings) == {"ETH", "LINK"}
    # ETH: 3.0 in - 1.0 out - 0.01 same-coin gas - 0.005 gas on the LINK send.
    assert _q(holdings["ETH"]["quantity"]) == _q("1.985")
    # LINK: 100 in - 40 out (its gas was paid in ETH, so LINK loses nothing to fees).
    assert _q(holdings["LINK"]["quantity"]) == _q("60")

    # The cached crypto_holdings table agrees, and the two positions are separate rows.
    eth = crypto.get_holding(conn, wallet, "ETH")
    link = crypto.get_holding(conn, wallet, "LINK")
    assert _q(eth["quantity"]) == _q("1.985")
    assert _q(link["quantity"]) == _q("60")

    # Coin-native invariants: the counterparty rode the Payee (decision 3), and NO
    # fiat/USD moved on any leg (decisions 4/5) -- the wallet holds no cash sleeve.
    credit = crypto.get_event(conn, eth_in)
    assert credit["payee"] == sender
    assert credit["amount"] is None          # no fiat leg on a wallet credit
    assert credit["price"] is None
    assert credit["basis"] is None

    debit = crypto.get_event(conn, link_out)
    assert debit["payee"] == recip_link
    assert debit["amount"] is None
    assert debit["fee_symbol"] == "ETH"      # the fee is a coin-native leg, not USD
    assert _q(debit["fee_quantity"]) == _q("0.005")
    assert debit["fee_amount"] is None

    assert crypto.crypto_cash(conn, wallet) == 0   # a wallet has no fiat cash sleeve
