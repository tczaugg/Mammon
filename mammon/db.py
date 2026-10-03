"""mammon.db -- schema definition and initialization for the Mammon database.

One SQLite file owns all data.

Money conventions (locked with the user):
- Cash amounts are signed INTEGER cents (negative = money out). No floats.
- Share quantities and per-share prices are TEXT-encoded Decimal values, so
  fractional shares and multi-decimal prices keep full precision.

Running balances use per-year rows in `balance_checkpoints` so a 40-year,
multi-account file opens and scrolls without summing every prior transaction.

Schema versioning uses SQLite's `PRAGMA user_version`. Each entry in
`MIGRATIONS` upgrades the database by exactly one version; `init_db` applies
every migration whose target version is newer than the file's current version,
so init is safe to call on a brand-new file or an existing one.
"""
from __future__ import annotations

import contextlib
import sqlite3          # type annotations only; see mammon.sqldriver
from pathlib import Path
from typing import Optional

from mammon import sqldriver

# ---------------------------------------------------------------------------
# Migration 1: initial schema
# ---------------------------------------------------------------------------
# Parent tables are declared before the tables that reference them. SQLite
# tolerates forward references, but ordering keeps the schema readable.
_V1 = """
CREATE TABLE accounts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL,          -- checking|savings|credit|cash|investment|asset|liability
    currency        TEXT NOT NULL DEFAULT 'USD',
    opening_balance INTEGER NOT NULL DEFAULT 0,   -- cents
    opening_date    TEXT,                          -- ISO YYYY-MM-DD
    institution     TEXT,
    note            TEXT,
    closed_flag     INTEGER NOT NULL DEFAULT 0,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE categories (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    type      TEXT,                          -- income|expense
    hidden    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name, parent_id)
);

CREATE TABLE payees (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    normalized_name TEXT
);

CREATE TABLE imports (
    id              INTEGER PRIMARY KEY,
    provider        TEXT,                    -- institution / source label
    source_format   TEXT,                    -- qif|ofx|qfx|json|csv
    filename        TEXT,
    file_hash       TEXT,                    -- for file-level dedup
    imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'pending',
    added_count     INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);

CREATE TABLE transactions (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date                TEXT NOT NULL,        -- ISO YYYY-MM-DD
    num                 TEXT,                 -- check number / reference
    payee               TEXT,
    category_id         INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    memo                TEXT,
    third_party         TEXT,                 -- actual counterparty behind Venmo/PayPal
    amount              INTEGER NOT NULL,     -- signed cents; negative = money out
    cleared             INTEGER NOT NULL DEFAULT 0,
    reconciled          INTEGER NOT NULL DEFAULT 0,
    transfer_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    transfer_pair_id    INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    import_id           INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    fitid               TEXT,                 -- source's unique id, for dedup
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE splits (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    amount         INTEGER NOT NULL,          -- signed cents
    memo           TEXT
);

CREATE TABLE balance_checkpoints (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    year       INTEGER NOT NULL,
    balance    INTEGER NOT NULL,              -- cents, end-of-year running balance
    PRIMARY KEY (account_id, year)
);

CREATE TABLE import_mappings (
    id                 INTEGER PRIMARY KEY,
    payee_pattern      TEXT NOT NULL UNIQUE,
    mapped_payee       TEXT,
    mapped_category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    source             TEXT,                  -- learned|user
    hit_count          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE transaction_matches (
    id              INTEGER PRIMARY KEY,
    imported_txn_id INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
    existing_txn_id INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
    score           REAL,
    approved        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE holdings (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol     TEXT NOT NULL,
    name       TEXT,
    quantity   TEXT NOT NULL DEFAULT '0',     -- Decimal text
    cost_basis INTEGER,                        -- cents
    UNIQUE(account_id, symbol)
);

CREATE TABLE price_history (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    close_price TEXT NOT NULL,                 -- Decimal text
    source      TEXT,
    UNIQUE(symbol, date)
);

CREATE TABLE investment_transactions (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date                TEXT NOT NULL,
    action              TEXT NOT NULL,         -- Buy|Sell|Div|ReinvDiv|IntInc|XIn|XOut|...
    symbol              TEXT,
    quantity            TEXT,                  -- Decimal text
    price               TEXT,                  -- Decimal text
    amount              INTEGER,               -- cents
    commission          INTEGER,               -- cents
    memo                TEXT,
    transfer_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    import_id           INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    fitid               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE reconciliations (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    statement_date    TEXT NOT NULL,
    statement_balance INTEGER NOT NULL,        -- cents
    reconciled_at     TEXT NOT NULL DEFAULT (datetime('now')),
    note              TEXT
);

CREATE INDEX idx_txn_account_date        ON transactions(account_id, date);
CREATE INDEX idx_txn_transfer_pair       ON transactions(transfer_pair_id);
CREATE INDEX idx_txn_fitid               ON transactions(fitid);
CREATE INDEX idx_splits_txn              ON splits(transaction_id);
CREATE INDEX idx_invtxn_account_date     ON investment_transactions(account_id, date);
CREATE INDEX idx_invtxn_fitid            ON investment_transactions(fitid);
CREATE INDEX idx_price_symbol_date       ON price_history(symbol, date);
CREATE INDEX idx_holdings_account        ON holdings(account_id);
"""

# ---------------------------------------------------------------------------
# Migration 2: per-transaction tag (Quicken's classification, distinct from the
# category). Added as a nullable column so existing files upgrade in place.
# ---------------------------------------------------------------------------
_V2 = """
ALTER TABLE transactions ADD COLUMN tag TEXT;
"""

# ---------------------------------------------------------------------------
# Migration 3: loan / liability domain (over-Quicken loan support).
#
# A liability/loan account can carry loan PARAMETERS: the original principal,
# term, scheduled payment amount + interval, an effective-dated interest-rate
# HISTORY (Quicken keeps a single rate; Mammon keeps every rate change so the
# amortization and the per-payment split stay correct across an ARM reset), and
# EXTRA amount lines each with its own category (escrow, PMI, HOA, ...). The
# amortization schedule and the principal/interest/escrow split are pure-domain
# computations in mammon.loans; these tables are only the persisted parameters.
#
# Money stays signed INTEGER cents. annual_rate is Decimal TEXT as an annual
# PERCENTAGE (e.g. '6.5' == 6.5% APR). Dates are ISO YYYY-MM-DD.
# ---------------------------------------------------------------------------
_V3 = """
CREATE TABLE loan_params (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    original_principal INTEGER NOT NULL,          -- cents
    origination_date   TEXT,                      -- ISO YYYY-MM-DD
    term_months        INTEGER NOT NULL,
    payment_amount     INTEGER NOT NULL,          -- cents, total scheduled payment (P&I + extras)
    payment_interval   TEXT NOT NULL DEFAULT 'monthly',
    note               TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE loan_rates (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    effective_date TEXT NOT NULL,                 -- ISO; this rate applies on/after this date
    annual_rate    TEXT NOT NULL,                 -- Decimal text, annual percentage ('6.5' = 6.5%)
    UNIQUE(account_id, effective_date)
);

CREATE TABLE loan_extras (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,                     -- escrow|PMI|HOA|... (posts to this category)
    amount      INTEGER NOT NULL,                  -- cents added to each payment
    label       TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_loan_rates_account  ON loan_rates(account_id, effective_date);
CREATE INDEX idx_loan_extras_account ON loan_extras(account_id);
"""

# ---------------------------------------------------------------------------
# Migration 4: scheduled/pending pre-entries. A loan payment can be PRE-ENTERED
# a few days before its due date as a pending split (principal + interest +
# escrow, from the amortization schedule); when the real bank payment later
# imports it MATCHES the pending row and posts into it instead of duplicating.
# ``scheduled=1`` marks a row that is a not-yet-posted placeholder; the merge
# flips it to 0. Nullable-by-default so existing files upgrade in place.
# ---------------------------------------------------------------------------
_V4 = """
ALTER TABLE transactions ADD COLUMN scheduled INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_txn_scheduled ON transactions(account_id, scheduled);
"""

# ---------------------------------------------------------------------------
# Migration 5: a split LINE may itself be a transfer to another account (Quicken
# allows an "[Account]" split leg -- a mortgage payment's principal posting to
# the loan/house account, a paycheck's 401(k) deferral posting to the retirement
# account). ``transfer_account_id`` names that counter-account (mutually
# exclusive with ``category_id``); ``transfer_pair_id`` points at the mirror
# transaction the leg creates in that account (NULL for an import-side leg whose
# mirror is imported from the counter-account's OWN register). Nullable columns
# so existing files upgrade in place.
# ---------------------------------------------------------------------------
_V5 = """
ALTER TABLE splits ADD COLUMN transfer_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL;
ALTER TABLE splits ADD COLUMN transfer_pair_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL;
"""

# ---------------------------------------------------------------------------
# Migration 6: online-banking account details. ``url`` holds the
# institution's login/download URL; ``account_number`` the (masked) statement
# account number; ``hidden`` drops an account from the left account bar WITHOUT
# closing it (a live-but-clutter account stays reconcilable from the Accounts
# list). All nullable / defaulted so existing files upgrade in place.
# ---------------------------------------------------------------------------
_V6 = """
ALTER TABLE accounts ADD COLUMN url TEXT;
ALTER TABLE accounts ADD COLUMN account_number TEXT;
ALTER TABLE accounts ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
"""

# ---------------------------------------------------------------------------
# Migration 7: automated-download script slot. ``download_script`` names the
# webSlinger automation Mammon runs to pull this account's statements (the
# per-account "Download" action + the Account-details download section). NULL
# until the user records/sets one; nullable so existing files upgrade in place.
# ---------------------------------------------------------------------------
_V7 = """
ALTER TABLE accounts ADD COLUMN download_script TEXT;
"""

# ---------------------------------------------------------------------------
# Migration 8: migration cutover (last-migrated) watermark. ``cutover_date`` is
# the ISO date of the newest transaction imported during the one-time full
# Quicken (QIF) history migration for the account. Migrated rows carry no fitid,
# so the first live OFX/QFX pull cannot dedup the overlap by fitid and would
# double-import it. Any incoming import row dated on/before this watermark is
# already represented in migrated history and is skipped (closes gap G4). NULL
# until the account is migrated; nullable so existing files upgrade in place.
# ---------------------------------------------------------------------------
_V8 = """
ALTER TABLE accounts ADD COLUMN cutover_date TEXT;
"""

# ---------------------------------------------------------------------------
# Migration 9: year-end holdings snapshots. The cash side already caches
# end-of-year running balances in ``balance_checkpoints``; this is its
# investment-position twin. A row is the full per-symbol replay state
# (:class:`mammon.investments._Position`) as of Dec 31 of ``year`` -- quantity,
# average-cost basis, cumulative dividends/realized, and ever_held -- so a
# position as of a later date can be computed from the prior year's snapshot
# plus only that year's transactions, instead of replaying from inception.
# ---------------------------------------------------------------------------
_V9 = """
CREATE TABLE IF NOT EXISTS holdings_checkpoints (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    year        INTEGER NOT NULL,
    symbol      TEXT    NOT NULL,
    quantity    TEXT    NOT NULL DEFAULT '0',
    cost_basis  INTEGER NOT NULL DEFAULT 0,
    dividends   INTEGER NOT NULL DEFAULT 0,
    realized    INTEGER NOT NULL DEFAULT 0,
    ever_held   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, year, symbol)
);
"""

# ---------------------------------------------------------------------------
# Migration 10: per-account webSlinger download field config. ``download_config``
# is a JSON object of the NON-SECRET input defaults the user saved in the
# Account-details Download section (e.g. {"userName": "1234567",
# "format": "OFX File (.OFX)"}), keyed by the script's declared input names. It
# prefills the Download dialog so a pull is one click. Secret inputs (MFA/TOTP)
# are one-time codes and are never stored. NULL until saved; nullable so existing
# files upgrade in place.
# ---------------------------------------------------------------------------
_V10 = """
ALTER TABLE accounts ADD COLUMN download_config TEXT;
"""

# ---------------------------------------------------------------------------
# Migration 11: persisted import-review rows. Freshly downloaded rows (webSlinger
# scrape rows classified NEW/MATCHING by :mod:`mammon.import_review`) used to live
# ONLY in the in-memory review panel, so a restart or account switch lost them.
# ``review_items`` persists each row so a pending review survives, drives the
# sidebar's pending indicator, and supports bulk Accept/Discard/Undo. Rows are
# deduped within an account by their source ``transaction_id`` (a partial unique
# index, so blank-id rows always insert); ``state`` walks pending -> accepted /
# discarded and remembers the txn it created/matched plus the undo info for a
# matched accept. ``raw_json`` keeps the original download dict verbatim.
# ---------------------------------------------------------------------------
_V11 = """
CREATE TABLE review_items (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    transaction_id  TEXT,                          -- source fitid/transactionId; dedupe key within account
    account_ref     TEXT,
    date            TEXT,
    amount          INTEGER,                        -- signed cents
    payee           TEXT,
    memo            TEXT,
    check_number    TEXT,
    is_transfer     INTEGER DEFAULT 0,
    transfer_account TEXT,
    raw_json        TEXT,                           -- original webSlinger row dict as JSON
    label           TEXT,                           -- 'NEW' | 'MATCHING'
    matched_txn_id  INTEGER,                        -- existing txn id when MATCHING (classify time)
    match_method    TEXT,                           -- 'transactionId' | 'date+amount' | 'manual' | ''
    state           TEXT DEFAULT 'pending',         -- 'pending' | 'accepted' | 'discarded'
    accepted_txn_id INTEGER,                        -- txn created (save_new) or matched (accept_match)
    prior_fitid     TEXT,                           -- undo info for an accepted MATCHING
    prior_cleared   INTEGER,
    created_at      TEXT
);
CREATE INDEX idx_review_account_state ON review_items(account_id, state);
CREATE UNIQUE INDEX idx_review_dedupe ON review_items(account_id, transaction_id)
    WHERE transaction_id IS NOT NULL AND transaction_id <> '';
"""

# ---------------------------------------------------------------------------
# Migration 12: classic payee renaming rules. When a user edits the
# provisional payee of a NEW import-review row, Mammon learns a keyword-based rule
# mapping a distinguishing keyword from the raw ``statementDescription`` to the
# user's clean payee (see :mod:`mammon.payee_rules`). Future imports whose bank
# text contains that keyword auto-fill the learned payee before the row is shown.
# ``keyword`` is stored UPPER-CASED and is unique (an edit re-teaches the same
# keyword by overwriting its payee). Rules are global (not per-account): a
# merchant's bank text means the same payee everywhere. ``match_count`` tallies
# auto-applications for the management surface.
# ---------------------------------------------------------------------------
_V12 = """
CREATE TABLE payee_rules (
    id          INTEGER PRIMARY KEY,
    keyword     TEXT NOT NULL,                      -- distinguishing token, UPPER-CASED
    payee       TEXT NOT NULL,                      -- clean payee to auto-fill
    match_count INTEGER NOT NULL DEFAULT 0,         -- times auto-applied
    created_at  TEXT,
    updated_at  TEXT
);
CREATE UNIQUE INDEX idx_payee_rules_keyword ON payee_rules(keyword);
"""

# ---------------------------------------------------------------------------
# Migration 13: category learning rules, the category-side twin of the payee
# renaming rules above. When a user sets/edits the category of a NEW
# import-review row, Mammon learns a keyword-based rule mapping a distinguishing
# keyword from the raw ``statementDescription`` to the chosen ``category_id``
# (see :mod:`mammon.category_rules`). Future imports whose bank text contains that
# keyword pre-fill the learned category before the row is shown. ``keyword`` is
# UPPER-CASED and unique; a multi-word keyword (e.g. "AMAZON WEB") is a
# refinement that out-ranks a broader single-token rule (longest keyword wins).
# ON DELETE CASCADE drops a rule if its category is removed.
# ---------------------------------------------------------------------------
_V13 = """
CREATE TABLE category_rules (
    id          INTEGER PRIMARY KEY,
    keyword     TEXT NOT NULL,                      -- distinguishing token(s), UPPER-CASED
    category_id INTEGER NOT NULL
                REFERENCES categories(id) ON DELETE CASCADE,
    match_count INTEGER NOT NULL DEFAULT 0,         -- times auto-applied
    created_at  TEXT,
    updated_at  TEXT
);
CREATE UNIQUE INDEX idx_category_rules_keyword ON category_rules(keyword);
"""

# ---------------------------------------------------------------------------
# Migration 14: transfer learning rules, the transfer-side twin of the payee
# and category rules above. When a user points a NEW import-review transfer row
# at a real account, Mammon learns a keyword-based rule mapping a distinguishing
# keyword from the raw ``statementDescription`` to the chosen account (see
# :mod:`mammon.transfer_rules`). Future imports whose bank text contains that
# keyword pre-fill the learned transfer account (as Quicken's ``[Account]``)
# before the row is shown -- the same "learn the first of each kind" gesture the
# payee/category engines give, now for transfers. ``keyword`` is
# UPPER-CASED and unique; a multi-word keyword is a refinement that out-ranks a
# broader single-token rule. ON DELETE CASCADE drops a rule if its account goes.
# ---------------------------------------------------------------------------
_V14 = """
CREATE TABLE transfer_rules (
    id                  INTEGER PRIMARY KEY,
    keyword             TEXT NOT NULL,                  -- distinguishing token(s), UPPER-CASED
    transfer_account_id INTEGER NOT NULL
                        REFERENCES accounts(id) ON DELETE CASCADE,
    match_count         INTEGER NOT NULL DEFAULT 0,     -- times auto-applied
    created_at          TEXT,
    updated_at          TEXT
);
CREATE UNIQUE INDEX idx_transfer_rules_keyword ON transfer_rules(keyword);
"""

# ---------------------------------------------------------------------------
# Migration 15: replace the flat ``payee_rules`` keyword table with an online-
# learning discriminative tree (see :mod:`mammon.rename_tree`). Payee renaming now
# learns from the user's corrections into a token trie that splits only where two
# merchants must be told apart, ranks tokens by inverse frequency, and gates
# auto-rename on node payee CARDINALITY (a single payee auto-renames; several
# offer a typeable dropdown). ``rename_nodes`` is the self-referencing
# trie (a node's ``token`` under its ``parent_id``; the root level is
# ``parent_id IS NULL``); ``rename_node_payees`` holds the payee(s) + hit count at
# each node; ``rename_token_freq`` is the frequency snapshot used for ranking;
# ``rename_stats`` tallies times-applied / times-overridden per payee for the
# management table; ``rename_meta`` stores the bootstrap flag. The old
# ``payee_rules`` table is RETIRED here -- the
# tree is bootstrapped from register history (raw memo -> chosen payee), not from
# the lossy keyword rows, so nothing of value is lost (user: no need to keep it).
# The category/transfer keyword tables are unaffected; they share the tokenizer
# via :mod:`mammon.keywords`, no longer via payee_rules.
# ---------------------------------------------------------------------------
_V15 = """
CREATE TABLE rename_nodes (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER REFERENCES rename_nodes(id) ON DELETE CASCADE,
    token      TEXT NOT NULL,                      -- edge label, UPPER-CASED
    depth      INTEGER NOT NULL                    -- 1 at the root level
);
CREATE INDEX idx_rename_nodes_parent ON rename_nodes(parent_id);
CREATE UNIQUE INDEX idx_rename_nodes_edge
    ON rename_nodes(IFNULL(parent_id, -1), token);

CREATE TABLE rename_node_payees (
    id       INTEGER PRIMARY KEY,
    node_id  INTEGER NOT NULL REFERENCES rename_nodes(id) ON DELETE CASCADE,
    payee    TEXT NOT NULL,                        -- clean payee learned here
    count    INTEGER NOT NULL DEFAULT 1            -- corroborating hits
);
CREATE UNIQUE INDEX idx_rename_node_payee ON rename_node_payees(node_id, payee);

CREATE TABLE rename_token_freq (
    token TEXT PRIMARY KEY,                        -- UPPER-CASED token
    freq  INTEGER NOT NULL DEFAULT 0               -- document frequency snapshot
);

CREATE TABLE rename_stats (
    payee            TEXT PRIMARY KEY,
    applied_count    INTEGER NOT NULL DEFAULT 0,   -- suggestion accepted
    overridden_count INTEGER NOT NULL DEFAULT 0    -- suggestion rejected for another
);

CREATE TABLE rename_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

DROP INDEX IF EXISTS idx_payee_rules_keyword;
DROP TABLE IF EXISTS payee_rules;
"""

# ---------------------------------------------------------------------------
# Migration 16: generalized scheduled/recurring payment DEFINITIONS. Loans have
# always been able to pre-enter upcoming payments (Migration 4's
# ``transactions.scheduled`` flag, driven by loan_params + the amortization
# schedule). This table generalizes that to any recurring bill -- subscriptions,
# fixed-price utilities (cable/internet), memberships, dues -- so Mammon can
# pre-enter them the same way and the user has ONE place (the Scheduled Payments
# manager) to view/add/edit/delete them.
#
# These rows are MAMMON-GENERATED definitions: they are NOT part of any imported
# QIF/OFX/CSV file (an import carries only posted transactions, never a "due next
# month" definition). The importers never read or write this table, so a
# definition SURVIVES a QIF re-import unchanged -- neither duplicated nor lost.
# The generator (``mammon.scheduled``) turns each active definition into pending
# ``scheduled=1`` pre-entry rows in ``transactions``. Columns nullable/defaulted
# where sensible so existing files upgrade in place; ON DELETE CASCADE drops a
# closed account's definitions with it.
# ---------------------------------------------------------------------------
_V16 = """
CREATE TABLE scheduled_payments (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payee        TEXT,
    amount       INTEGER NOT NULL,            -- signed cents (a bill is negative)
    frequency    TEXT NOT NULL DEFAULT 'monthly',
    next_date    TEXT NOT NULL,               -- ISO YYYY-MM-DD of the next occurrence
    category_id  INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    memo         TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_scheduled_payments_account ON scheduled_payments(account_id, active);
"""

# ---------------------------------------------------------------------------
# Migration 17: give legacy transfer legs a DEFINITE cleared/reconciled status.
# Before create_transfer carried the Quicken `C` flag through the double-entry
# collapse, an imported transfer's two legs arrived at cleared=0/reconciled=0 --
# so their Clr column rendered BLANK, standing out amid an otherwise
# fully-reconciled historical ledger (the user's QIFs mark transfers cleared/
# reconciled the same as everything else). The columns were never NULL (both are
# NOT NULL DEFAULT 0), but 0/0 is indistinguishable from "no status" on screen.
#
# These are historical, already-settled transfers between the user's OWN
# accounts (import_id set), so stamp them reconciled -- a definite, non-blank
# status consistent with the surrounding ledger. Legs that already carry a
# status (the asymmetric one-sided legs, which always carried the QIF flag, and
# anything a user has reconciled by hand) are left untouched. The two legs stay
# INDEPENDENTLY reconcilable -- this only fills in the ones that were blank.
# Idempotent: re-running matches nothing once the legs are non-zero.
# ---------------------------------------------------------------------------
_V17 = """
UPDATE transactions
   SET cleared = 1, reconciled = 1
 WHERE transfer_account_id IS NOT NULL
   AND import_id IS NOT NULL
   AND cleared = 0
   AND reconciled = 0;
"""

# ---------------------------------------------------------------------------
# Migration 18: make a loan's escrow / PMI / extra amounts EFFECTIVE-DATED, so
# they carry a history exactly like loan_rates does. Before this a loan_extras
# row was a single flat "current amount" -- changing escrow overwrote it, losing
# what the old amount was and when it changed, so a re-derived split could not
# reproduce a payment posted under the prior amount. ``effective_date`` gives
# each amount an on/after date (mirroring loan_rates.effective_date); a category
# with several rows is a timeline, and the amount in force on a payment's date is
# the latest row whose effective_date is on/before it. Existing rows are
# backfilled to the loan's origination date (empty string -> "from the very
# beginning" when a loan has no origination), so they keep applying from the
# start exactly as before. The index mirrors idx_loan_rates_account; uniqueness
# per (account, category, date) is enforced in loans.add_extra_change rather than
# by a DB constraint, so this migration cannot fail on any pre-existing data.
# Nullable-add + backfill so existing files upgrade in place.
# ---------------------------------------------------------------------------
_V18 = """
ALTER TABLE loan_extras ADD COLUMN effective_date TEXT;
UPDATE loan_extras
   SET effective_date = COALESCE(
       (SELECT origination_date FROM loan_params
         WHERE loan_params.account_id = loan_extras.account_id), '')
 WHERE effective_date IS NULL;
CREATE INDEX idx_loan_extras_hist ON loan_extras(account_id, category, effective_date);
"""

# ---------------------------------------------------------------------------
# _V19: dated total-payment history. When escrow, the rate, or an extra changes,
# the whole scheduled payment changes too; the new total is first-class DATED
# data (like the rate/escrow history) so a projected schedule uses the amount in
# force on each period's date. Backfill every existing loan's current
# payment_amount at its origination date, so files upgrade in place with the
# same schedule they had before (earlier dates fall back to this baseline row).
# ---------------------------------------------------------------------------
_V19 = """
CREATE TABLE loan_payments (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    effective_date TEXT NOT NULL,
    amount INTEGER NOT NULL,
    UNIQUE(account_id, effective_date)
);
INSERT INTO loan_payments(account_id, effective_date, amount)
    SELECT account_id, COALESCE(origination_date, ''), payment_amount
      FROM loan_params;
CREATE INDEX idx_loan_payments_hist ON loan_payments(account_id, effective_date);
"""

# ---------------------------------------------------------------------------
# Migration 20: un-reconcile transfer legs that were AUTO-marked reconciled
# without an actual reconcile action. Migration 17 stamped legacy imported
# transfer legs cleared=1/reconciled=1 to fill their blank Clr, and an older
# QIF collapse could carry a source 'R' onto a transfer leg. Under the agreed
# behavior a transfer is NEVER auto-reconciled: each leg starts uncleared and
# is reconciled INDEPENDENTLY in its own account's reconcile flow. An
# auto-marked leg is hidden from the reconcile dialog (unreconciled_rows
# excludes reconciled rows) and cannot be unmarked, so reset it to uncleared
# so it reappears and becomes reconcilable.
#
# Preserve genuine status WHERE DISTINGUISHABLE: only reset a leg whose account
# has NO reconciliations row -- i.e. the user never actually ran a reconcile
# there, so its reconciled=1 can only be an auto-mark. Legs in an account the
# user genuinely reconciled are left untouched. Each leg is judged by its OWN
# account, so the two legs stay independent. Plain (non-transfer) rows are never
# touched. Idempotent: re-running matches nothing once the auto-marked legs are
# back at 0/0.
# ---------------------------------------------------------------------------
_V20 = """
UPDATE transactions
   SET cleared = 0, reconciled = 0
 WHERE transfer_account_id IS NOT NULL
   AND reconciled = 1
   AND NOT EXISTS (
       SELECT 1 FROM reconciliations r
        WHERE r.account_id = transactions.account_id
   );
"""

# ---------------------------------------------------------------------------
# Migration 21: one-time DEDUPE of doubled scheduled loan-payment rows. The
# balance-driven recast/regenerate work (dated loan_payments, _V19) had a path
# that re-materialised a loan's scheduled principal legs from a change's
# effective date forward WITHOUT first clearing the prior generation, so on the
# LOAN side every regenerated payment landed twice while the mirrored checking
# leg (a one-sided transfer, transfer_pair_id NULL) was untouched -- i.e. the
# loan register showed each payment doubled. This collapses those exact
# duplicates in place.
#
# A duplicate is TWO OR MORE rows in the SAME loan account (JOIN loan_params)
# that agree on (date, amount, transfer_account_id) AND are bare scheduled
# principal legs -- scheduled=0, NO splits, a one-sided transfer leg
# (transfer_account_id set, transfer_pair_id NULL). Exactly ONE survivor is kept
# per group; the rest are deleted. The survivor is the row that (in order)
# bears a reconciled mark, then a cleared mark, then came from a real import
# (import_id set), then the lowest id -- so genuine reconciles/clears and the
# authoritative imported leg are preserved.
#
# SAFE BY CONSTRUCTION:
#  * Only LOAN accounts are touched (the JOIN), where two identical same-date
#    principal legs can only be a regeneration artifact, never two real payments.
#  * transfer_pair_id IS NULL is required, so a genuine paired transfer -- e.g. a
#    principal-only paydown that owns a real mirror leg -- is never deleted and
#    no mirror is ever orphaned (deleted rows own no counterpart).
#  * Rows carrying splits (normal split-posted payments) are excluded.
#  * Grouping on amount means the FINAL/balloon payment and any principal-only
#    extra (unique amount for their date) form singleton groups and are kept.
#  * Two same-date rows with DIFFERENT amounts are DIFFERENT groups -> both kept
#    (an ambiguous rate/recast change is left for the user, never guessed away).
# Idempotent: once one row per group remains, a re-run matches nothing.
# ---------------------------------------------------------------------------
_V21 = """
WITH cand AS (
    SELECT t.id, t.account_id, t.date, t.amount, t.transfer_account_id,
           t.reconciled AS rec, t.cleared AS cl,
           CASE WHEN t.import_id IS NOT NULL THEN 1 ELSE 0 END AS has_import
      FROM transactions t
      JOIN loan_params lp ON lp.account_id = t.account_id
     WHERE t.scheduled = 0
       AND t.transfer_pair_id IS NULL
       AND t.transfer_account_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)
),
keep AS (
    SELECT c.id FROM cand c
     WHERE c.id = (
         SELECT c2.id FROM cand c2
          WHERE c2.account_id = c.account_id
            AND c2.date = c.date
            AND c2.amount = c.amount
            AND c2.transfer_account_id = c.transfer_account_id
          ORDER BY c2.rec DESC, c2.cl DESC, c2.has_import DESC, c2.id ASC
          LIMIT 1
     )
)
DELETE FROM transactions
 WHERE id IN (SELECT id FROM cand)
   AND id NOT IN (SELECT id FROM keep);
"""

# ---------------------------------------------------------------------------
# Migration 22: collapse the DIFFERENT-AMOUNT doubled loan-payment legs that _V21
# leaves behind. In a real model each loan payment posts on the FUNDING account
# (checking) as a split whose principal leg transfers INTO the loan; the loan side
# is only the one-sided mirror that split creates (splits.transfer_pair_id -> a
# loan leg whose own transfer_pair_id stays NULL). An older, reverted regenerate
# path also left a SECOND one-sided leg for the same payment (a stale imported
# [Loan] principal leg, or a prior mirror), so the loan register showed each
# payment doubled -- and after a New Payment / escrow recast the two legs carry
# DIFFERENT amounts (e.g. a stale import leg 50657 beside the live mirror 60626),
# which _V21 (grouped on (date, amount)) cannot collapse.
#
# This deletes a one-sided loan leg that is provably the stale duplicate: bare (no
# split of its own), NOT itself a live split mirror, NOT a two-sided transfer (so
# no counterpart is ever orphaned), AND with ANOTHER leg on the same loan account
# and date that IS a live split mirror (the survivor is that mirror -- the leg the
# checking split actually points at). Grouping on DATE (not amount) is what catches
# the recast case. A LONE one-sided leg with no live-mirror twin is kept untouched
# -- it is the only record of that payment, never a duplicate. Mirrors this repair
# in loans_schedule.dedupe_loan_payment_legs (called on every Edit-Loan save), so a
# file both self-heals on open and stays deduped as payments change. Idempotent.
# ---------------------------------------------------------------------------
_V22 = """
DELETE FROM transactions WHERE id IN (
  SELECT t.id FROM transactions t
    JOIN loan_params lp ON lp.account_id = t.account_id
   WHERE t.scheduled = 0
     AND t.transfer_account_id IS NOT NULL
     AND t.transfer_pair_id IS NULL
     AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)
     AND t.id NOT IN (SELECT transfer_pair_id FROM splits
                       WHERE transfer_pair_id IS NOT NULL)
     AND EXISTS (SELECT 1 FROM transactions m
                  WHERE m.account_id = t.account_id AND m.date = t.date
                    AND m.id <> t.id
                    AND m.id IN (SELECT transfer_pair_id FROM splits
                                  WHERE transfer_pair_id IS NOT NULL))
);
"""

# ---------------------------------------------------------------------------
# Migration 23: carry INVESTMENT rows through the import-review queue. A file
# import (CSV/OFX) now lands in review exactly like a scraped/downloaded batch --
# never straight into the register -- and an investment row needs its security
# fields to survive the review round-trip (persist -> load -> accept), so
# review_items grows the investment columns. Existing cash review rows default
# is_investment=0 and leave the new columns NULL. Idempotent (guarded by
# user_version like every migration).
# ---------------------------------------------------------------------------
_V23 = """
ALTER TABLE review_items ADD COLUMN is_investment INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_items ADD COLUMN action TEXT;
ALTER TABLE review_items ADD COLUMN symbol TEXT;
ALTER TABLE review_items ADD COLUMN quantity TEXT;
ALTER TABLE review_items ADD COLUMN price TEXT;
ALTER TABLE review_items ADD COLUMN commission INTEGER;
"""

# ---------------------------------------------------------------------------
# Migration 24: let a loan name its OWN interest-expense category. The
# computed-interest split line used to be hard-coded to a single constant
# ("Interest Exp"), but the right category differs per loan (the user's recast
# Recast posts interest to "Int Exp", a rental to "Landlord:Int Exp"). The loan
# setup wizard now captures an interest-expense category alongside the rate
# history and persists it here; the split generator posts interest to THIS
# category, falling back to the module default only when it is NULL. Existing
# loans default to NULL (fallback), preserving today's behavior. Nullable add,
# idempotent (user_version-guarded like every migration).
# ---------------------------------------------------------------------------
_V24 = """
ALTER TABLE loan_params ADD COLUMN interest_category TEXT;
"""

# ---------------------------------------------------------------------------
# Migration 25: remember the prior RECONCILED flag when a review row is accepted
# against an existing register line, so undo restores it exactly. accept_match
# now RAISES the matched txn's reconciled flag to carry the incoming QIF 'C'
# (X/R) status -- a ~30-year archive of reconciled transfers previously matched
# here and lost their R because accept only ever forced cleared=1. To undo that
# faithfully we must snapshot the pre-accept reconciled value alongside the
# existing prior_cleared. Nullable add, idempotent (user_version-guarded).
# ---------------------------------------------------------------------------
_V25 = """
ALTER TABLE review_items ADD COLUMN prior_reconciled INTEGER;
"""

# ---------------------------------------------------------------------------
# Migration 26: saved column-map profiles for the generic tabular importer
# (mammon.importers.tabular). A downloaded/scraped delimited source is
# fingerprinted by its header signature; the first import of a new/changed format
# shows the user a preview + optional wizard, and the accepted column map is saved
# here so later imports of that same format need no prompt. ``config`` is a JSON
# object {"roles": {...CashRoles...}, "account_type": "..."}; ``signature`` is the
# sha1 of the normalized non-empty header names. OFX/QIF never touch this table.
# ---------------------------------------------------------------------------
_V26 = """
CREATE TABLE import_profiles (
    id         INTEGER PRIMARY KEY,
    signature  TEXT NOT NULL UNIQUE,
    name       TEXT,
    config     TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

# ---------------------------------------------------------------------------
# Migration 27: persist the review row's ``payee_supplied`` flag. A file-parsed
# record whose payee arrived VERBATIM on the source (an OFX <NAME>, a QIF payee,
# or a tabular importer's explicit payee column such as a Venmo From/To) is
# authoritative: import_review.predict_fields must NOT re-derive it from the
# statement description. That flag lived only in memory, so persist_entries ->
# load_pending (the path EVERY file/download review takes before display) dropped
# it and the row reloaded with payee_supplied=0 -- predict_fields then ran the
# rename tree over the memo and clobbered a good payee (user: Venmo 'From' names
# were mangled to the note text). Persist it so the authoritative-payee signal
# survives the reload. Existing pending rows default 0 (re-import to refresh);
# scrape rows correctly stay 0 so their payee keeps re-querying the rename tree.
# ---------------------------------------------------------------------------
_V27 = """
ALTER TABLE review_items ADD COLUMN payee_supplied INTEGER NOT NULL DEFAULT 0;
"""

# ---------------------------------------------------------------------------
# Migration 28: drop the unused ``third_party`` column.
#
# The column was added in v1 to record the real counterparty behind an
# intermediary ("Payee = Venmo, third_party = Jane Doe"). In practice a far
# simpler model won: give the intermediary its own ACCOUNT, so money moving
# between it and a bank account is an ordinary transfer and the real payee/memo
# live on the intermediary's own register rows. That needs no extra field, scales
# past a handful of payments a month, and reuses the transfer machinery that is
# already tested. The column never held a value in practice, so this drop is not
# lossy.
# ---------------------------------------------------------------------------
_V28 = """
ALTER TABLE transactions DROP COLUMN third_party;
"""

# ---------------------------------------------------------------------------
# Migration 29: group review rows into IMPORT BATCHES.
#
# A batch is ONE import operation, which may span several FILES: a webSlinger
# download can pull a set of files in one go, and an institution that will not
# export an arbitrary date range has to be fetched piecemeal (typically a file
# per month). Selecting several files in the import picker is likewise one
# batch. All of those belong together and are reviewed together.
#
# Batches also give review retention a meaningful unit. Elapsed time is NOT a
# valid metric here -- a user can legitimately go months without touching the
# register, and that gap says nothing about whether the review data is still
# useful. What makes an old review stale is SUBSEQUENT ACTIVITY: once a couple
# more periods have been imported into the same account, the older rows have
# stopped being ground truth anyone refers back to. Retention therefore keeps
# the newest N batches PER ACCOUNT and drops the rest.
# ---------------------------------------------------------------------------
_V29 = """
CREATE TABLE import_batches (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    source      TEXT,                        -- 'import' | 'download'
    file_count  INTEGER NOT NULL DEFAULT 0,
    note        TEXT                         -- the file name(s) behind the batch
);

CREATE INDEX idx_import_batches_account ON import_batches(account_id, id);

ALTER TABLE review_items ADD COLUMN batch_id INTEGER REFERENCES import_batches(id);

CREATE INDEX idx_review_items_batch ON review_items(account_id, batch_id);
"""

# ---------------------------------------------------------------------------
# Migration 30: keep an IN-PROGRESS reconcile's statement inputs.
#
# A reconcile is not one sitting. The user closes the window to look something
# up in the register -- a charge they do not recognize, a payment that has not
# arrived -- and comes back; before this table that round trip threw away the
# statement date, the ending balance and the whole credit-card block, and they
# were re-typed from the paper statement every time.
#
# The `reconciliations` table cannot carry this: it is the record of COMPLETED
# reconciliations, and `last_reconciliation` reads it to seed the next one. A
# draft is the opposite -- provisional, one per account, deleted the moment
# `finish_reconciliation` locks the statement in.
#
# `finance_txn_id` is the load-bearing column. The finance charge is posted as a
# real cleared transaction, and the dialog re-finds it by id to update it rather
# than post a second one. Losing that id on close meant reopening, re-entering
# the same finance charge, and silently DUPLICATING the charge in the register.
# ON DELETE SET NULL so deleting the charge in the register is not a dangling
# reference -- the dialog then posts a fresh one, which is the right recovery.
# ---------------------------------------------------------------------------
_V30 = """
CREATE TABLE reconcile_drafts (
    account_id        INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    statement_date    TEXT    NOT NULL DEFAULT '',
    beginning_cents   INTEGER NOT NULL DEFAULT 0,
    ending_cents      INTEGER NOT NULL DEFAULT 0,
    charges_cents     INTEGER NOT NULL DEFAULT 0,
    payments_cents    INTEGER NOT NULL DEFAULT 0,
    credits_cents     INTEGER NOT NULL DEFAULT 0,
    finance_cents     INTEGER NOT NULL DEFAULT 0,
    finance_category  TEXT    NOT NULL DEFAULT '',
    finance_txn_id    INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# ---------------------------------------------------------------------------
# Migration 31: SUPERSEDED BY 32 -- do not copy this design.
#
# Added a period-start column on a misdiagnosis. A card reconcile showing four
# out-of-period payments as cleared looked like a missing lower bound; the actual
# cause was a statement date stored with the wrong YEAR, which made the existing
# upper bound select months that were not on the statement. Quicken asks for no
# start date on any reconcile, and all past uncleared items belong in the window.
# Migration 32 drops the column. Kept in the list because live databases applied
# it -- migrations are never edited or removed once real files have run them.
#
# Original note follows.
#
# Migration 31: a credit-card reconcile draft remembers its PERIOD START.
#
# A card statement covers a billing period, and its implied beginning balance is
# what was owed when that period OPENED. Items dated before it are already inside
# that figure, so counting them again as "cleared" double-counts them -- a real
# ledger opened its August reconcile showing $6,500.00 cleared with nothing
# checked, being four payments from four EARLIER statements.
#
# Bank accounts need no such bound: there the beginning covers only reconciled
# rows, so an older still-unreconciled item is genuinely outstanding and must
# keep appearing. The column is nullable, and a NULL period start means the
# unbounded behavior (everything through the statement date).
# ---------------------------------------------------------------------------
_V31 = """
ALTER TABLE reconcile_drafts ADD COLUMN period_start TEXT NOT NULL DEFAULT '';
"""

# ---------------------------------------------------------------------------
# Migration 32: drop the period-start column added by 31.
#
# A reconcile window is everything not yet reconciled through the statement
# date -- past uncleared items included, which is the point: they are still
# outstanding and still need to be found. Bounding it below hid them.
# ---------------------------------------------------------------------------
_V32 = """
ALTER TABLE reconcile_drafts DROP COLUMN period_start;
"""

# ---------------------------------------------------------------------------
# Migration 33: entropy-ranked discrimination + a second learned tree for ACTIONS.
#
# Measured on a long ledger's (description -> payee) pairs, the rename
# tree's inverse-global-frequency ranking had two structural faults:
#
#   * mixed alphanumeric tokens (ISINs, auth codes, masked ids like x*****99)
#     are 87% single-occurrence noise, yet being rare they ranked FIRST and
#     became unreachable root nodes;
#   * frequency is the wrong axis entirely -- "IAT" appears 373 times but maps
#     to 3 payees (highly discriminative), while "VENMO" appears 282 times and
#     maps to 73 payees (discriminates nothing). What matters is the label
#     entropy H(label | token), not how often the token occurs.
#
# The snapshot therefore now stores per-token label ENTROPY alongside document
# frequency, and ranking sorts pure-and-supported tokens first. Replayed online
# over the ledger, this cut wrong auto-renames from 4.2% to 1.8% at identical
# auto coverage while shrinking "no suggestion" from 38% to 23%.
#
# The action_* tables are a parallel tree instance mapping a source's raw
# activity text ("Credit Interest", "RECORDKEEPING FEE") onto Quicken actions --
# the same importer-guessing problem as payee renaming, but with a finite label
# set: bootstrapped from 5,619 labeled investment rows it reaches 88% correct
# auto-mapping (4.2% wrong) vs 76%/8.5% under frequency ranking. The label
# column is named `payee` so both trees share one SQL vocabulary; here it holds
# the ACTION.
# ---------------------------------------------------------------------------
_V33 = """
ALTER TABLE rename_token_freq ADD COLUMN entropy REAL;

CREATE TABLE action_nodes (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER REFERENCES action_nodes(id) ON DELETE CASCADE,
    token      TEXT NOT NULL,
    depth      INTEGER NOT NULL
);
CREATE INDEX idx_action_nodes_parent ON action_nodes(parent_id);
CREATE UNIQUE INDEX idx_action_nodes_edge
    ON action_nodes(IFNULL(parent_id, -1), token);

CREATE TABLE action_node_labels (
    id      INTEGER PRIMARY KEY,
    node_id INTEGER NOT NULL REFERENCES action_nodes(id) ON DELETE CASCADE,
    payee   TEXT NOT NULL,                  -- the ACTION label (shared SQL shape)
    count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(node_id, payee)
);

CREATE TABLE action_token_freq (
    token   TEXT PRIMARY KEY,
    freq    INTEGER NOT NULL DEFAULT 0,
    entropy REAL
);
"""

# Ordered list; index i upgrades the DB from version i to version i+1.
# ---------------------------------------------------------------------------
# Migration 34: a stock split's EXACT ratio, as a numerator/denominator pair.
#
# A split was stored only as ``quantity`` = new shares per TEN old (Quicken's
# encoding: a 2-for-1 is 20), and the replay multiplied by quantity/10. That
# form cannot hold a 4-for-3 or a 1-for-3 -- 10*4/3 has no terminating decimal
# -- so those splits were unrecordable, which is absurd: the storage encoding
# was dictating which corporate actions the user is allowed to own.
#
# The pair is authoritative when present and is applied as ``qty * num / den``,
# multiplying BEFORE dividing, so 300 shares at 4:3 give exactly 400 rather than
# 300 * 1.3333... ``quantity`` remains as a derived legacy value, and every
# reader falls back to it when the pair is NULL -- no backfill is needed, since
# every ratio the old encoding could express converts cleanly on read
# (investments.split_factor).
# ---------------------------------------------------------------------------
_V34 = """
ALTER TABLE investment_transactions ADD COLUMN split_num INTEGER;
ALTER TABLE investment_transactions ADD COLUMN split_den INTEGER;
"""

# ---------------------------------------------------------------------------
# Migration 35: scheduled payments become REMINDERS (parity roadmap item 5).
# ``transfer_account_id`` makes a definition a transfer (the pre-entry gets a
# mirror in that account, like any transfer); ``lead_days`` is this
# definition's own "remind / pre-enter N days in advance" (NULL = the default);
# ``auto_enter`` = 0 means remind only -- the row waits for the user's Enter
# instead of being pre-entered as a placeholder.
# ---------------------------------------------------------------------------
_V35 = """
ALTER TABLE scheduled_payments ADD COLUMN transfer_account_id INTEGER
    REFERENCES accounts(id) ON DELETE SET NULL;
ALTER TABLE scheduled_payments ADD COLUMN lead_days INTEGER;
ALTER TABLE scheduled_payments ADD COLUMN auto_enter INTEGER NOT NULL DEFAULT 1;
"""

# ---------------------------------------------------------------------------
# Migration 36: a loan knows which account PAYS it. The user's real model (and
# Quicken's) posts the whole mortgage payment on checking as a split -- interest,
# escrow, and a principal leg transferring to the loan -- with only the principal
# mirror on the loan register. A pre-entry must take that shape on THAT account
# or the checking download can never merge into it. NULL means "infer it from
# history" (loans.funding_account); the Loan Setup wizard stores it explicitly.
# ---------------------------------------------------------------------------
_V36 = """
ALTER TABLE loan_params ADD COLUMN funding_account_id INTEGER
    REFERENCES accounts(id) ON DELETE SET NULL;
"""

# Tax lots (roadmap item 7). ``accounts.lot_method`` names how a disposal is
# costed (average | fifo | lifo; NULL = average, the behavior every earlier
# figure was computed under). ``holdings_checkpoints.lots`` carries the open
# lots at each year end as JSON, so the snapshot+delta replay reproduces the
# from-inception lot state exactly; the existing snapshots hold no lots and are
# dropped here -- a read without snapshots is a from-inception replay, always
# correct, and the next rebuild writes them back. ``lot_assignments`` are the
# lots a user named for one sale (Quicken's Specify Lots), which win over the
# method's order. ``securities`` is what allocation groups by.
_V37 = """
ALTER TABLE accounts ADD COLUMN lot_method TEXT;
ALTER TABLE holdings_checkpoints ADD COLUMN lots TEXT;
CREATE TABLE lot_assignments (
    id          INTEGER PRIMARY KEY,
    sale_txn_id INTEGER NOT NULL REFERENCES investment_transactions(id) ON DELETE CASCADE,
    lot_txn_id  INTEGER NOT NULL REFERENCES investment_transactions(id) ON DELETE CASCADE,
    quantity    TEXT NOT NULL
);
CREATE INDEX idx_lot_assignments_sale ON lot_assignments(sale_txn_id);
CREATE TABLE securities (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    sec_type    TEXT,
    asset_class TEXT
);
DELETE FROM holdings_checkpoints;
"""

# Predictions from history (calendar and projection): a payee the app noticed
# recurring can be dismissed per account, so the guess is not shown again.
_V38 = """
CREATE TABLE prediction_dismissals (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payee_key    TEXT NOT NULL,
    payee        TEXT,
    dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, payee_key)
);
"""

# Budgets (roadmap item 6, first slice: schema + domain layer, no UI yet).
# A ``budget`` is a named, toggleable envelope set; its ``budget_lines`` carry
# one target ``amount_cents`` per (category, month) so a plan can differ month
# to month without a row per day. ``period`` is the ISO ``'YYYY-MM'`` month the
# line applies to. ``rollover`` marks a line whose unspent remainder should
# carry into the next period -- persisted now, consumed by a later slice; the
# first slice compares a single period only. The UNIQUE key is the upsert
# target so re-setting a line's amount overwrites rather than duplicates.
# FKs cascade so deleting a budget (or a category) cannot orphan lines. Actuals
# are never stored here -- they are derived read-only from ``transactions`` via
# ``reports.spending``; ``mammon.ledger`` remains the sole writer of txn rows.
_V39 = """
CREATE TABLE budgets (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE budget_lines (
    id           INTEGER PRIMARY KEY,
    budget_id    INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    period       TEXT NOT NULL,            -- ISO 'YYYY-MM'
    amount_cents INTEGER NOT NULL,         -- signed cents; the month's target
    rollover     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(budget_id, category_id, period)
);
CREATE INDEX idx_budget_lines_budget_period ON budget_lines(budget_id, period);
"""

# Rule conditions (roadmap item 10, first slice: schema + domain layer, no UI
# yet). ``category_rules`` and ``transfer_rules`` learn a ``keyword -> value``
# association; this slice lets a rule ALSO carry optional, ANDed conditions so a
# keyword can be narrowed to one account, a signed-cent amount range, and/or a
# case-insensitive memo substring. All four columns are NULLABLE and default
# NULL, so every existing rule keeps matching exactly as before -- a rule with
# all-NULL conditions IS a plain keyword rule. The matcher evaluates a condition
# only when it is given transaction context (account_id, amount_cents, memo);
# with no context the match is byte-identical to today, so existing callers that
# pass no context are unaffected. ``account_id`` here is the account a
# transaction lives in (the SCOPE), deliberately distinct from
# ``transfer_rules.transfer_account_id`` (the transfer's other side / payload);
# it ON DELETE SET NULL so removing the scope account un-scopes the rule rather
# than destroying its still-valid payload. ``amount_min_cents`` /
# ``amount_max_cents`` are an inclusive SIGNED integer-cent range (negative =
# money out). ``mammon.ledger`` remains the sole writer of transaction rows;
# these tables hold no money movement.
_V40 = """
ALTER TABLE category_rules ADD COLUMN account_id INTEGER
    REFERENCES accounts(id) ON DELETE SET NULL;
ALTER TABLE category_rules ADD COLUMN amount_min_cents INTEGER;
ALTER TABLE category_rules ADD COLUMN amount_max_cents INTEGER;
ALTER TABLE category_rules ADD COLUMN memo_contains TEXT;
ALTER TABLE transfer_rules ADD COLUMN account_id INTEGER
    REFERENCES accounts(id) ON DELETE SET NULL;
ALTER TABLE transfer_rules ADD COLUMN amount_min_cents INTEGER;
ALTER TABLE transfer_rules ADD COLUMN amount_max_cents INTEGER;
ALTER TABLE transfer_rules ADD COLUMN memo_contains TEXT;
"""

# Asset allocation beyond the brokerage (SRD 5.8d, portfolio.allocation). An
# ACCOUNT can now carry an asset class of its own, so a house, a rental or a
# piece of equipment takes its place in the allocation beside the securities.
# Quicken cannot do this -- its allocation is investment accounts only, and the
# standing advice in its own forums is to fake a property with a dummy security
# in a dummy brokerage -- but a person who owns a house does not think of it as
# outside their assets. NULL means "not said": a cash-shaped account (checking,
# savings, cash) still counts as cash without anyone typing anything, while an
# asset account stays UNCLASSIFIED until the user classifies it, because what an
# "other asset" account holds (a house, a car, a violin) cannot be guessed from
# the ledger. Liabilities are not allocated at all -- an allocation is of what
# you own; debt belongs to net worth.
_V41 = """
ALTER TABLE accounts ADD COLUMN asset_class TEXT;
"""

# What a non-investment asset is WORTH, as opposed to what it cost (SRD 5.8e).
# A property account's ledger balance is its cost basis: purchase price plus the
# improvements posted to it. That is the right number for a capital gain and the
# wrong number for every question about allocation, leverage or net worth, and
# until now it was the only number the file had -- a house bought in 2010 was
# carried at its 2010 price against a mortgage that had been amortizing for
# fifteen years, so the leverage read far worse than it was.
#
# So market value gets its own series, shaped exactly like ``price_history`` is
# for securities: one row per (account, date), a source, and an optional note.
# Cost basis stays in the ledger, market value lives here, and the two are never
# conflated -- which is the flaw in Quicken's "Update Account Balance" adjusting
# transaction, where an appreciation is indistinguishable from money spent on a
# new roof.
#
# ``accounts.property_address`` is what a valuation source looks the account up
# by. It is NULLABLE and must be CONFIRMED by the user, never derived from the
# account name: a name is not an address ("Condo (Asset)"), a name that looks
# like one may be incomplete ("240 Birch" needs a city), and a neighbouring
# account's address is not this account's -- inferring 118 Cedar from a
# liability of that name returned a real, correctly-scraped $400,000 for a house
# the user had sold years earlier. Same discipline as `investments.ticker_of`:
# suggest, confirm, store, never fire on a guess.
_V42 = """
CREATE TABLE asset_values (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,                 -- ISO YYYY-MM-DD
    value_cents INTEGER NOT NULL,              -- market value, signed cents
    source      TEXT,                          -- 'manual' | 'zillow' | ...
    note        TEXT,
    UNIQUE(account_id, date)
);
CREATE INDEX idx_asset_values_account_date ON asset_values(account_id, date);
ALTER TABLE accounts ADD COLUMN property_address TEXT;
"""

# Which asset a loan is secured BY (SRD 5.8e). Without this, `asset_values.exposure`
# had to be handed the debt by its caller, because nothing in the file related
# "240 Birch Loan" to "240 Birch" -- and inferring the pair from name
# similarity is the same class of error as inferring a property's address from
# its account name (which returned a real, correctly-scraped valuation for a
# house the user had sold years earlier).
#
# The column lives on the LIABILITY and points at the asset, because the real
# relationship is MANY loans to ONE property: a first and second mortgage, or an
# original loan plus its refinance, all sit against one house (this user has
# `ANYTOWN House Loan` + `Loan2`, and `Riverbend Loan` + `Riverbend
# Recast`). Putting it on the asset would model only the single-loan case.
#
# NULLABLE, so every existing liability keeps working as an unsecured one, and
# ON DELETE SET NULL so removing a property un-links its loans rather than
# cascading a delete into rows that still hold real payment history.
_V43 = """
ALTER TABLE accounts ADD COLUMN secured_by_account_id INTEGER
    REFERENCES accounts(id) ON DELETE SET NULL;
"""

# A TARGET asset mix, and the bands that say when the real one has drifted far
# enough to act on (SRD 5.8f). The pie answers "where is my money"; this answers
# "is it where I meant it to be", which is the question an allocation view exists
# to serve -- classes are separated because they behave differently, and the
# payoff of holding several is keeping them at chosen weights as they diverge.
#
# Shaped like `budgets`/`budget_lines`, the established pattern here: several
# NAMED targets, at most one active, lines keyed by asset class. Percentages are
# Decimal-encoded TEXT for the same reason share prices are -- a mix is a
# precise quantity the user typed, and float drift in a number that must sum to
# 100 is exactly the kind of error that shows up as a phantom 0.01% deviation.
#
# ``sleeve`` records WHICH accounts the target governs. It defaults to
# `investments` because a target mix is about invested money: including a
# chequing balance that swings with the month's bills manufactures drift the user
# cannot act on, and no one rebalances by selling 5% of a house. Property is
# reported alongside as CONTEXT, never as part of the mix being corrected.
#
# ``band_abs_pct`` / ``band_rel_pct`` default to 5 and 25 -- the 5/25 rule. One
# absolute threshold alone is wrong at both ends: 5 points never fires on a 4%
# sleeve that has doubled, and a purely relative band fires constantly on a 60%
# one. Whichever triggers first wins.
_V44 = """
CREATE TABLE allocation_targets (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 0,
    sleeve       TEXT NOT NULL DEFAULT 'investments',
    band_abs_pct TEXT NOT NULL DEFAULT '5',
    band_rel_pct TEXT NOT NULL DEFAULT '25'
);
CREATE TABLE allocation_target_lines (
    id          INTEGER PRIMARY KEY,
    target_id   INTEGER NOT NULL REFERENCES allocation_targets(id) ON DELETE CASCADE,
    asset_class TEXT NOT NULL,
    pct         TEXT NOT NULL,             -- Decimal text, percent of the sleeve
    UNIQUE(target_id, asset_class)
);
CREATE INDEX idx_allocation_target_lines_target ON allocation_target_lines(target_id);
"""


# Migration 45: first-class, many-per-transaction tags.
#
# The original v2 `transactions.tag` was a single free-text column -- one string
# per row, no way to carry two labels ("vacation" AND "reimbursable") or to ask
# "show me everything tagged vacation" without a substring search that also hits
# payees and memos. This introduces a proper `tags` table and a
# `transaction_tags` junction so a transaction can carry any number of tags and a
# tag can span any number of transactions.
#
# `transactions.tag` is NOT dropped: it stays as a normalized, comma-joined CACHE
# of a row's tag names, written in lockstep with the junction by ledger.set_tags
# (the sole writer). Keeping it means every existing reader -- the register cell,
# the global Find, the report line loader (reports/_lines.py), find-and-replace --
# keeps working untouched, while the junction is the authoritative store for exact
# per-tag filtering and reporting. The cache is a projection, never a second
# source of truth: on any tag write the junction is rebuilt first, then the cache
# is recomputed from it.
#
# `name` collates NOCASE so "Home" and "home" are one tag. ON DELETE CASCADE on
# transaction_id retires a deleted transaction's (and both transfer legs') tag
# links automatically -- foreign_keys is ON (see connect()).
#
# Data migration: carry each non-empty legacy tag forward as one first-class tag
# (legacy semantics were single-valued, so the whole trimmed string is one tag),
# cross-link it, and trim the cache column so it equals the tag name it stands
# for. Whitespace-only legacy tags become NULL.
_V45 = """
CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id         INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (transaction_id, tag_id)
);
CREATE INDEX idx_transaction_tags_tag ON transaction_tags(tag_id);
UPDATE transactions SET tag = NULL WHERE tag IS NOT NULL AND TRIM(tag) = '';
INSERT OR IGNORE INTO tags(name)
    SELECT DISTINCT TRIM(tag) FROM transactions
    WHERE tag IS NOT NULL AND TRIM(tag) <> '';
INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id)
    SELECT t.id, g.id FROM transactions t
    JOIN tags g ON g.name = TRIM(t.tag)
    WHERE t.tag IS NOT NULL AND TRIM(t.tag) <> '';
UPDATE transactions SET tag = TRIM(tag)
    WHERE tag IS NOT NULL AND TRIM(tag) <> '';
"""

# ASSET-CLASS MIXTURE per security (SRD 5.8g). One security is not always one
# class: a target-date fund is roughly 58% equity / 40% bonds / 2% cash, and
# counting the whole position under a single class does not merely coarsen the
# allocation -- it makes every drift figure in `mammon.rebalance` WRONG, because
# the misallocated weight is subtracted from some other class that then reads
# underweight by exactly that amount.
#
# One row per (symbol, class). Percentages are Decimal-encoded TEXT for the same
# reason target weights are. ``source`` records where the split came from
# ('yfinance', 'manual') and ``as_of`` when, because a fund's mixture drifts with
# its own holdings -- a two-year-old split is a stale fact, not a wrong one, and
# the difference is worth showing.
#
# A security with no rows here keeps its single ``securities.asset_class`` and
# behaves exactly as before, so this is purely additive: nothing changes until a
# mixture is fetched or entered.
_V46 = """
CREATE TABLE security_mixtures (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    pct         TEXT NOT NULL,             -- Decimal text, percent of the position
    source      TEXT,
    as_of       TEXT,
    UNIQUE(symbol, asset_class)
);
CREATE INDEX idx_security_mixtures_symbol ON security_mixtures(symbol);
"""

# PER-ACCOUNT NATIVE CURRENCY + an FX-rate store, so net worth can be reported
# per-currency and (optionally) totalled through FX into one presentation
# currency. `accounts.currency` already exists (ISO 4217, NOT NULL DEFAULT
# 'USD') from the initial schema, so there is nothing to ALTER -- an account
# with no explicit currency IS USD, the base. This migration only adds the rate
# store.
#
# `fx_rates` keeps one row per (date, base, quote): `rate` is Decimal-encoded
# TEXT -- units of `quote` per 1 unit of `base` on `date` -- never a float, for
# the same reason `price_history.close_price` and share quantities are text.
# `mammon.fx.get_rate` reads the most recent rate on/before a date and derives
# the inverse when only one direction was recorded, so storing 'USD'->'EUR' also
# answers 'EUR'->'USD'. FX adds NO transaction write path: `mammon.ledger` stays
# the sole writer of transaction rows and cash stays signed integer cents.
_V47 = """
CREATE TABLE fx_rates (
    date  TEXT NOT NULL,                 -- ISO YYYY-MM-DD
    base  TEXT NOT NULL,                 -- ISO 4217, e.g. 'USD'
    quote TEXT NOT NULL,                 -- ISO 4217, e.g. 'EUR'
    rate  TEXT NOT NULL,                 -- Decimal text: units of `quote` per 1 `base`
    PRIMARY KEY (date, base, quote)
);
"""

# A SPLIT TEMPLATE for a scheduled-payment definition. A recurring paycheck or
# bill often is not one category but a fixed breakdown (gross/taxes/deferrals;
# electric+water). When "Schedule <payee>" is launched from the calendar on a
# PREDICTED entry whose history is split, the definition LEARNS that split (the
# same lines the register's "Copy from previous <payee> split" would copy) and
# reproduces it as split lines every time a pre-entry is generated. These rows
# are the DEFINITION's template only; the actual `splits` rows on a pre-entered
# transaction are still written solely by `mammon.ledger.set_splits`, keeping
# ledger the one writer of transaction/split rows. A definition with no rows
# here behaves exactly as before (single amount + category). Mirrors the
# `splits` columns so a learned line can carry a category OR a transfer leg.
_V48 = """
CREATE TABLE scheduled_splits (
    id                  INTEGER PRIMARY KEY,
    scheduled_id        INTEGER NOT NULL REFERENCES scheduled_payments(id) ON DELETE CASCADE,
    category_id         INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    transfer_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    amount              INTEGER NOT NULL,          -- signed cents
    memo                TEXT
);
CREATE INDEX idx_scheduled_splits_def ON scheduled_splits(scheduled_id);
"""

# A security's TICKER, as its source stated it -- not as anything derived it.
# Quicken's ``!Type:Security`` block carries three separate fields and always
# has: ``N`` the name ("VGT VANGUARD INFO TECH ETF"), ``S`` the ticker ("VGT")
# and ``T`` the type ("Stock"). The importer parsed all three, used ``S`` only
# to translate the price section's ticker back to a name, and discarded it --
# nothing ever wrote the ``securities`` table at all. So the file kept only the
# concatenated name, and when a 2026 Interactive Brokers CSV supplied the bare
# ticker there was nowhere to put it except a SECOND security: one ETF under two
# spellings, its dividends divorced from its position (SRD 5.8e-2).
#
# The ABSENCE of ``S`` is information too, and is why this column is worth a
# migration rather than a heuristic. Quicken omits it for a plan's internal
# funds -- "FID BALANCED K6", "INTL EQUITY INDEX" -- which have no public ticker,
# and those are exactly the names ``investments.ticker_of`` guesses wrong: it
# returns INTL, a real listed company whose prices are already in this file. 533
# of 614 securities in the reference export carry an explicit ticker, so for
# those the answer is recorded fact and no one has to confirm a guess.
_V49 = """
ALTER TABLE securities ADD COLUMN ticker TEXT;
"""

# How well a price is KNOWN, for the prices this app derives rather than reads.
#
# A plan fund quoted nowhere public gets its only prices from its own fee rows:
# 0.007 shares removed for $1.01 implies $144.29 a share. That number is not
# wrong, it is IMPRECISE -- the value is rounded to the cent and the share count
# to the millishare, so the true price lies somewhere in [134.00, 156.15], and a
# second fee the same day (0.001 shares for $0.24) implies $240.00 with a far
# wider interval still. Rejecting such prices was considered and is worse than
# the disease: without them a dormant 401(k) holds its last contribution price
# for six years and then revalues in a single day by the whole of that gap,
# which is exactly the
# bug that led here. A noisy price beats a six-year flat line.
#
# So the estimate is KEPT and its uncertainty is RECORDED, which is the honest
# form of the same information: the chart draws the interval as a band, and a
# reader can see at a glance that a fee-derived point is worth less trust than a
# quote. NULL bounds mean exactly known -- every quote, every stated price -- so
# nothing existing changes and no band is drawn for it.
_V50 = """
ALTER TABLE price_history ADD COLUMN price_low TEXT;
ALTER TABLE price_history ADD COLUMN price_high TEXT;
"""

# Migrations 51-53: cryptocurrency support. A crypto wallet is a new account
# type ('crypto'), classified investment-like for net worth / grouping / the
# allocation pie but stored as a DISTINCT type value. `accounts.type` is
# free-text (no CHECK constraint), so introducing the type needs no migration;
# the wallet address reuses the existing `accounts.account_number` column (which
# the MCP authorizer already blanks), so no new sensitive column appears. These
# three tables are the crypto twins of `investment_transactions` / `holdings` /
# `holdings_checkpoints`, kept SEPARATE because a coin position is not an equity
# lot: quantities are wei-scale (up to 18 decimals), the event taxonomy adds
# swaps, on-chain transfers, staking/airdrop/mining income and network gas fees
# that have no equity analogue, and the natural exact-dedup key is an on-chain
# `tx_hash`. Quantities and per-unit prices are Decimal-encoded TEXT (exact at
# any decimal count); the fiat that moves is signed integer cents.
# `mammon/crypto.py` is the SOLE writer of these tables (mirroring
# investments.py's single-writer discipline); ledger.py stays the only writer of
# the cash `transactions` table. Price history is REUSED, namespaced by the
# yfinance pair symbol ('BTC-USD', 'ETH-USD') to avoid a coin/stock collision,
# so `price_history` needs no change.
#
# 51 -- the single event log, one row per single-asset delta. A wallet-to-wallet
# move is the transfer mirror model again (two legs linked by `transfer_pair_id`,
# each `transfer_account_id` pointing at the other wallet); a coin-for-coin swap
# links its SWAP_OUT + SWAP_IN legs within one account by `swap_group_id`.
# `tx_hash` is the exact-dedup key (fitid's role for chain activity); `fitid`
# covers exchange rows that carry no chain hash.
_V51 = """
CREATE TABLE crypto_transactions (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date                TEXT NOT NULL,             -- ISO YYYY-MM-DD (storage/domain always ISO)
    action              TEXT NOT NULL,             -- BUY|SELL|SWAP_OUT|SWAP_IN|TRANSFER_OUT|
                                                   --   TRANSFER_IN|SEND|RECEIVE|REWARD|INTEREST|
                                                   --   AIRDROP|MINING|FEE|FORK
    symbol              TEXT,                      -- coin/token symbol, e.g. 'ETH', 'BTC', 'USDC'
    quantity            TEXT,                      -- Decimal text, signed (out = negative), wei-scale
    price               TEXT,                      -- Decimal text, per-unit USD FMV (nullable)
    amount              INTEGER,                   -- signed cents: fiat that moved (buy/sell), else 0
    basis               INTEGER,                   -- cents: cost basis of acquired qty (nullable)
    fee_symbol          TEXT,                      -- coin the network fee was paid in
    fee_quantity        TEXT,                      -- Decimal text, network fee quantity (nullable)
    fee_amount          INTEGER,                   -- cents, USD value of the fee (nullable)
    transfer_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    transfer_pair_id    INTEGER,                   -- links the two mirror legs of an own-wallet transfer
    swap_group_id       INTEGER,                   -- links SWAP_OUT + SWAP_IN legs in one account
    tx_hash             TEXT,                      -- on-chain tx hash: the natural exact-dedup key
    memo                TEXT,
    import_id           INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    fitid               TEXT,                      -- exchange-supplied id when there is no chain hash
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 52 -- the replay caches. `crypto_holdings` is the current position per
# (account, symbol), the crypto twin of `holdings`. `crypto_holdings_checkpoints`
# is the per-(account, year, symbol) replay snapshot, the exact analogue of
# `holdings_checkpoints`, so a 40-year multi-wallet file opens and scrolls fast:
# a position as of a later date is computed from the prior year's snapshot plus
# only that year's rows. `income` is staking/airdrop/mining income to date (the
# 'dividends' analogue); `lots` is the JSON lot list (same shape investments use).
_V52 = """
CREATE TABLE crypto_holdings (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol     TEXT NOT NULL,
    name       TEXT,
    quantity   TEXT NOT NULL DEFAULT '0',          -- Decimal text
    cost_basis INTEGER,                             -- cents
    UNIQUE(account_id, symbol)
);
CREATE TABLE crypto_holdings_checkpoints (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    year       INTEGER NOT NULL,
    symbol     TEXT    NOT NULL,
    quantity   TEXT    NOT NULL DEFAULT '0',        -- Decimal text
    cost_basis INTEGER NOT NULL DEFAULT 0,          -- cents
    income     INTEGER NOT NULL DEFAULT 0,          -- staking/airdrop/mining income to date
    realized   INTEGER NOT NULL DEFAULT 0,          -- realized gain/loss to date
    ever_held  INTEGER NOT NULL DEFAULT 0,
    lots       TEXT,                                -- JSON lot list (FIFO/spec-ID)
    PRIMARY KEY (account_id, year, symbol)
);
"""

# 53 -- indexes, kept a SEPARATE migration so the CREATE TABLE statements above
# are never edited after the fact. `(account_id, date)` for register/replay
# scans, `tx_hash` and `fitid` for exact-dedup lookups on import.
_V53 = """
CREATE INDEX idx_crypto_txn_account_date ON crypto_transactions(account_id, date);
CREATE INDEX idx_crypto_txn_tx_hash      ON crypto_transactions(tx_hash);
CREATE INDEX idx_crypto_txn_fitid        ON crypto_transactions(fitid);
CREATE INDEX idx_crypto_holdings_account ON crypto_holdings(account_id);
"""

# 54 -- completes first-class tags (parity roadmap item 12).
#
# Three gaps closed at once, all in the same feature:
#
# `splits.tag_id` -- a tag PER SPLIT LEG. `transaction_tags` is keyed on the
# transaction, so until now a tag could only describe a whole row. Quicken tags
# a split leg (`SBusiness:Consulting/Project A`), and that is the case the tag exists
# to serve: one parts order split across several projects. Unioning those onto
# the parent would credit the WHOLE order to every project in it, silently
# overstating each one in a by-tag spending report -- the same double-count the
# transfer rules exist to prevent. A leg carries at most one tag because that is
# all QIF can express; the many-per-ROW model is unchanged and still lives in
# `transaction_tags`.
#
# `tags.color` -- a hex string (`#3b6ea5`), NULL meaning "not chosen", which the
# UI fills from the shared categorical palette. Colors are per-tag identity, so
# a tag keeps its color between the register, the By Tag report and chart
# wedges, where color previously followed a slice's RANK and changed as the
# ranking moved.
#
# `tags.description` -- Quicken's tag list carries one (`!Type:Tag` N/D pairs),
# and discarding it on import is the bug this migration's own import fix is
# about; there is no reason to drop it a second time on the way in.
_V54 = """
ALTER TABLE splits ADD COLUMN tag_id INTEGER REFERENCES tags(id) ON DELETE SET NULL;
CREATE INDEX idx_splits_tag ON splits(tag_id);
ALTER TABLE tags ADD COLUMN color TEXT;
ALTER TABLE tags ADD COLUMN description TEXT;
"""

# ---------------------------------------------------------------------------
# v55 -- the per-payee category tree (mammon.category_tree).
#
# The flat ``category_rules`` keyword table learns a rule from ONE correction,
# keyed on the first non-noise token of the raw text, matched GLOBALLY. Replayed
# over a new user's first year (625 accepted rows) it fired on 67% of rows and
# was wrong on 26% of those, and half of the errors proposed a category never
# seen for that payee -- a rule learned from one merchant firing on an unrelated
# one. The town name "ANYTOWN" (in the tail of every local
# card swipe) became a Utilities:Gas & Electric rule and then fired on Subway,
# O'Reilly, an auto shop and a community theater.
#
# The replacement scopes every decision to the resolved payee: one small
# discrimination tree per payee, over the SOURCE TEXT tokens, so a candidate
# category can only ever be one that payee has actually carried.
#
#   category_nodes        the trie; one root per normalized payee, children keyed
#                         by the token that split them
#   category_node_labels  category vote counts at a node
#   category_payee_stats  category vote counts for the payee overall -- the
#                         coherence gate and the dropdown ranking read this
#   category_token_freq   token -> (freq, label entropy) snapshot for ranking
# ---------------------------------------------------------------------------
_V55 = """
CREATE TABLE category_nodes (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES category_nodes(id) ON DELETE CASCADE,
    payee_key TEXT NOT NULL,                       -- normalized payee (the root)
    token     TEXT                                 -- NULL on a root node
);
CREATE INDEX idx_category_nodes_payee ON category_nodes(payee_key);
CREATE UNIQUE INDEX idx_category_nodes_child
    ON category_nodes(parent_id, token);

CREATE TABLE category_node_labels (
    id          INTEGER PRIMARY KEY,
    node_id     INTEGER NOT NULL REFERENCES category_nodes(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_category_node_label
    ON category_node_labels(node_id, category_id);

CREATE TABLE category_payee_stats (
    id          INTEGER PRIMARY KEY,
    payee_key   TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_category_payee_stat
    ON category_payee_stats(payee_key, category_id);

CREATE TABLE category_token_freq (
    token   TEXT PRIMARY KEY,
    freq    INTEGER NOT NULL DEFAULT 0,
    entropy REAL
);

"""


# ---------------------------------------------------------------------------
# v56 -- the per-token category distribution ``category_token_freq.entropy`` is
# computed from.
#
# It has to be stored, not derived on demand: ranking decides which token a trie
# walk descends on, so it must stay correct as rows are learned ONE AT A TIME
# during a review session, not only when a snapshot is rebuilt. Without it a
# freshly learned token has NULL entropy, every token ties, ranking degenerates
# to frequency, and the Costco trie splits on the token COSTCO -- which both
# branches carry and which therefore discriminates nothing, sending
# "COSTCO WHSE ..." down the fuel branch.
#
# This is a SEPARATE migration rather than an edit to _V55 because a real
# database had already applied _V55 by the time the gap was found. IF NOT EXISTS
# so a database created from either shape upgrades cleanly.
# ---------------------------------------------------------------------------
_V56 = """
CREATE TABLE IF NOT EXISTS category_token_labels (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_category_token_label
    ON category_token_labels(token, category_id);
"""


# ---------------------------------------------------------------------------
# v57 -- purge every learned category rule.
#
# ``category_rules`` was grown one row per correction by
# ``category_rules.learn_from_edit``, keyed on the first non-noise token of the
# raw text and matched GLOBALLY. Replayed over a real first year it fired on 67%
# of rows and was wrong on 26% of those; the town name in the tail of every local
# card swipe ("ANYTOWN", the local town) became a Utilities:Gas & Electric
# rule that then fired on Subway, O'Reilly, an auto shop and a community
# theater. Every row in a real ledger came from that learner -- on the reference
# ledger every one did: many carry multi-token keywords, which only
# ``refine_keyword`` produces and which the Rules manager's Add dialog cannot
# create, and none carries a condition, which only that editor can set.
#
# Auto-categorization now lives in the per-payee tree (mammon.category_tree,
# v55/v56) and nothing writes this table any more. Keeping the old rows would
# leave the broken engine's output quietly influencing predictions forever, so
# they go. The TABLE stays: it is now hand-authored only, which is what makes a
# rule in it a deliberate instruction rather than a guess.
# ---------------------------------------------------------------------------
_V57 = """
DELETE FROM category_rules;
"""


# ---------------------------------------------------------------------------
# v58 -- delete review rows that cannot be transactions.
#
# A webSlinger script returns one array per extraction, and the gather-every-list
# fallback in ``webslinger._rows_from`` concatenated ALL of them. Anytown Credit Union's
# script declares "The subAccountList maps shortName to accountId", so its
# lookup table -- ``{"id": 1234567, "shortName": "Checking"}`` -- was flattened in
# with the transactions and became review rows with no date, no amount and no
# text: eleven blank lines at the top of the user's review list on a fresh
# reload. ``_rows_from`` now tests each array's row SHAPE (a per-bank key name
# cannot be blacklisted) and ``import_review.build_review`` drops such rows as a
# backstop, but the ones already stored have to go.
#
# Deliberately narrow: a row is removed only when it has NO date, NO amount, NO
# payee, NO memo and NO check number, is not an investment row, and was never
# accepted. A zero-amount row that carries a date or a description is a real,
# reviewable transaction and stays.
# ---------------------------------------------------------------------------
_V58 = """
DELETE FROM review_items
 WHERE COALESCE(date,'') = ''
   AND COALESCE(amount,0) = 0
   AND COALESCE(payee,'') = ''
   AND COALESCE(memo,'') = ''
   AND COALESCE(check_number,'') = ''
   AND COALESCE(is_investment,0) = 0
   AND accepted_txn_id IS NULL;
"""


# ---------------------------------------------------------------------------
# v59 -- remember a matched row's PRIOR amount.
#
# Accepting a match now adopts the bank's amount (see
# ``import_review.accept_match``): the register line may be a scheduled
# pre-entry whose figure is a forecast -- the finance calendar's median, or an
# amortization row whose escrow has since moved -- and the bank's number is the
# one that actually happened. The payee and the category/split stay as the user
# has them.
#
# Undo therefore has to be able to put the old amount back, alongside the
# fitid/cleared/reconciled it already restores, or ``unmatch_one`` would leave
# the register holding a value the user never entered and could not recover.
# ---------------------------------------------------------------------------
_V59 = """
ALTER TABLE review_items ADD COLUMN prior_amount INTEGER;
"""


# ---------------------------------------------------------------------------
# v60 -- the rename EXAMPLE LOG replaces the online rename/action tries.
#
# Payee renaming and investment-action mapping are now a decision tree rebuilt
# from the user's accepted corrections at every prediction
# (:mod:`mammon.rename_tree`); nothing is learned online any more, so the trie
# tables (``rename_nodes``, ``rename_node_payees``, ``rename_token_freq`` and
# their ``action_*`` twins) go. What replaces them is the CORPUS: one row per
# accepted review row -- the source text, the source's own payee field, the
# label the user chose, and the id of the transaction the accept created. The
# label is read LIVE through that id (a payee edited in the register is the
# correction of a correction); the stored one is the fallback once the
# transaction is gone.
#
# This table is what review retention must never touch. ``review_items`` keeps
# three batches or a year, whichever is more, and then purges; a rename taught
# from a row that has since been purged -- an annual bill -- lives here.
#
# Seeded from the accepted review rows on hand. Only CORRECTIONS are seeded for
# payees: a row whose payee is its own description (case-insensitively -- the
# title-cased default the register shows) or its supplied payee field was kept
# as-is, and the loader would drop it anyway. Investment accepts seed the
# action domain; the review row's ``action`` is the importer's raw guess and
# rides along as ``extra`` (the loader blanks it when it is already a canonical
# action). The old tries are not converted: the reference ledger's held over a
# thousand payees bootstrapped from hand-typed memos -- names the user never
# entered, which this rewrite exists to stop. ``rename_stats`` / ``rename_meta`` stay.
# ---------------------------------------------------------------------------
_V60 = """
CREATE TABLE rename_examples (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,                  -- 'payee' | 'action'
    txn_id     INTEGER,                        -- transactions.id / investment_transactions.id (live label)
    review_id  INTEGER,                        -- the review row it came from (informational; may be purged)
    text       TEXT NOT NULL DEFAULT '',       -- the bank's description, verbatim
    extra      TEXT NOT NULL DEFAULT '',       -- the source's own payee field / raw action text
    label      TEXT NOT NULL,                  -- what the user chose at accept time (fallback)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_rename_examples_kind ON rename_examples(kind, id);
CREATE INDEX idx_rename_examples_txn ON rename_examples(kind, txn_id);

INSERT INTO rename_examples(kind, txn_id, review_id, text, extra, label, created_at)
SELECT 'payee', t.id, ri.id, COALESCE(ri.memo, ''),
       CASE WHEN COALESCE(ri.payee_supplied, 0) = 1 THEN COALESCE(ri.payee, '') ELSE '' END,
       t.payee, COALESCE(ri.created_at, datetime('now'))
  FROM review_items ri JOIN transactions t ON t.id = ri.accepted_txn_id
 WHERE ri.state = 'accepted' AND ri.label = 'NEW'
   AND COALESCE(ri.is_investment, 0) = 0 AND COALESCE(ri.is_transfer, 0) = 0
   AND COALESCE(TRIM(t.payee), '') <> ''
   AND LOWER(TRIM(t.payee)) <> LOWER(TRIM(COALESCE(ri.memo, '')))
   AND LOWER(TRIM(t.payee)) <> LOWER(TRIM(COALESCE(ri.payee, '')))
 ORDER BY ri.accepted_txn_id;

INSERT INTO rename_examples(kind, txn_id, review_id, text, extra, label, created_at)
SELECT 'action', it.id, ri.id, COALESCE(ri.memo, ''), COALESCE(ri.action, ''),
       it.action, COALESCE(ri.created_at, datetime('now'))
  FROM review_items ri JOIN investment_transactions it ON it.id = ri.accepted_txn_id
 WHERE ri.state = 'accepted' AND ri.label = 'NEW' AND COALESCE(ri.is_investment, 0) = 1
   AND COALESCE(TRIM(it.action), '') <> ''
 ORDER BY ri.accepted_txn_id;

DROP TABLE IF EXISTS rename_node_payees;
DROP TABLE IF EXISTS rename_nodes;
DROP TABLE IF EXISTS rename_token_freq;
DROP TABLE IF EXISTS action_node_labels;
DROP TABLE IF EXISTS action_nodes;
DROP TABLE IF EXISTS action_token_freq;
DELETE FROM rename_meta WHERE key = 'ranking_version';
"""


# Migration 61: the crypto redesign (SRD §5.8h; design locked in review 55a2d8a0).
# Two crypto account KINDS share ``type='crypto'`` and are BOTH multi-token and valued
# like securities (a quantity per token, priced at market):
#   * 'wallet'   -- a single address / paper wallet holding coins/ERC-20 tokens ONLY,
#                   no fiat. Coin arrives/leaves; the network fee is paid IN the coin.
#   * 'exchange' -- coins PLUS fiat currencies: the existing BUY/SELL/SWAP model with
#                   an internal USD cash sleeve.
# ``crypto_kind`` is a REAL typed column carrying that choice explicitly -- NOT an
# overloaded nullable "native coin" flag (the design's earlier nullable-column /
# JSON-header modeling was rejected as unclear). Existing crypto accounts predate the
# split and were built on the exchange model, so they backfill to 'exchange'; a NULL
# ``crypto_kind`` now means "not a crypto account", never "exchange".
#
# ``crypto_transactions.payee`` is the counterparty that IS the payee/payer: the
# on-chain ``From`` address on a coin increase, the ``To`` address on a coin decrease
# (there is no separate "counterparty" concept). It is machine text that renders as a
# distinct Payee column, so it gets its own column rather than colliding with the
# user's own (routinely blanked) memo. Both adds are nullable and append-only; no
# existing migration is edited, and neither ALTER duplicates an existing column
# (``crypto_kind`` is new to ``accounts``; ``payee`` is new to ``crypto_transactions``).
_V61 = """
ALTER TABLE accounts ADD COLUMN crypto_kind TEXT;
UPDATE accounts SET crypto_kind = 'exchange' WHERE type = 'crypto';
ALTER TABLE crypto_transactions ADD COLUMN payee TEXT;
"""

# review_items grows the COIN-NATIVE columns, so a crypto wallet's import lands in
# the same review queue every other source does instead of being forced through the
# cash shape. It was the cash shape that produced the reported defect: an Etherscan
# by-address CSV reviewed as cash mapped Blockno into `amount` and rendered a Cash
# Bal column, for an account where no USD ever moves. Real typed columns, never a
# serialized blob, because the review panel's bulk operations are SQL over this
# table. `symbol`/`quantity`/`action`/`payee`/`memo`/`date` are reused as-is (they
# already mean the right thing); only the coin fee legs and the on-chain identity
# are new. `is_crypto` selects the coin-native accept path the way `is_investment`
# selects the securities one.
_V62 = """
ALTER TABLE review_items ADD COLUMN is_crypto INTEGER NOT NULL DEFAULT 0;
ALTER TABLE review_items ADD COLUMN fee_symbol TEXT;
ALTER TABLE review_items ADD COLUMN fee_quantity TEXT;
ALTER TABLE review_items ADD COLUMN tx_hash TEXT;
"""

# Migration 63: a per-account audit log of every change made to a RECONCILED
# transaction. A reconciled row should almost never change -- once it is locked
# in against a statement, editing its amount or date, or deleting it outright,
# silently throws off the next reconcile of that account, and the failure then
# shows up far from its cause. This table records one row per changed field on an
# edit (operation='edit') and one row per surviving value on a deletion
# (operation='delete') of a transaction that was reconciled at the time, so such
# a change can be traced after the fact and a broken reconcile diagnosed.
#
# ledger.py (the sole writer of transaction rows) writes this table from its
# edit and delete paths; everything else reads it (ledger.reconciled_change_log).
# ``transaction_id`` intentionally carries NO foreign key: after a delete the row
# it names is gone, and the log entry must outlive it -- a cascade would erase
# exactly the evidence this log exists to keep.
_V63 = """
CREATE TABLE reconciled_change_log (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    transaction_id INTEGER,                                   -- may be gone after a delete; no FK on purpose
    changed_at     TEXT NOT NULL DEFAULT (datetime('now')),   -- ISO timestamp
    operation      TEXT NOT NULL,                             -- 'edit' | 'delete'
    field          TEXT,                                      -- column name changed
    old_value      TEXT,
    new_value      TEXT
);
CREATE INDEX idx_reconciled_change_log_account ON reconciled_change_log(account_id, id);
"""


# A security whose ticker was renamed keeps ONE continuous identity: the old
# ticker becomes an alias of the surviving (canonical) symbol instead of having
# its price_history and transactions rewritten. Reads union an alias's rows into
# the canonical identity (mammon.investments.resolve_symbol), so history stays
# continuous across the rename date without touching a single historical row.
# The alias points AT an existing securities row (the canonical); the alias
# spelling itself need not have a securities row (a ticker may retire).
_V64 = """
CREATE TABLE security_aliases (
    alias_symbol     TEXT PRIMARY KEY,
    canonical_symbol TEXT NOT NULL REFERENCES securities(symbol)
);
CREATE INDEX idx_security_aliases_canonical ON security_aliases(canonical_symbol);
"""


# 65: share reconciliation (SRD 5.11b). The share balance is to a security what
# the cash balance is to a bank account, and a 401(k) of untickered internal
# funds is the case that forces it: no quote source exists, so the statement's
# share count is the ONLY truth available. This mirrors the cash reconcile
# shapes exactly -- cleared/reconciled flags on the rows plus a finished-period
# record and a resumable draft -- one period per (account, security).
#   * quantities are Decimal-encoded TEXT, never floats (SRD 5.8);
#   * `symbol` holds the CANONICAL symbol (a renamed security reconciles as one
#     identity across its security_aliases spellings);
#   * adjustment_txn_id links the share-adjustment row the user accepted to
#     close an unexplained gap, so it stays identifiable and deletable. Deleting
#     it does NOT unwind this row -- the period must then be reconciled by hand.
# UNIQUE(account_id, symbol, statement_date) is what makes finishing the same
# period twice an upsert rather than a second, conflicting record.
_V65 = """
CREATE TABLE share_reconciliations (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol             TEXT NOT NULL,
    statement_date     TEXT NOT NULL,
    starting_qty       TEXT NOT NULL,            -- Decimal text
    ending_qty         TEXT NOT NULL,            -- Decimal text
    adjustment_txn_id  INTEGER REFERENCES investment_transactions(id) ON DELETE SET NULL,
    note               TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, symbol, statement_date)
);
CREATE INDEX idx_share_recon_account ON share_reconciliations(account_id, symbol);
CREATE TABLE share_reconcile_drafts (
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol            TEXT    NOT NULL,
    statement_date    TEXT    NOT NULL DEFAULT '',
    starting_qty      TEXT    NOT NULL DEFAULT '',
    ending_qty        TEXT    NOT NULL DEFAULT '',
    starting_price    TEXT    NOT NULL DEFAULT '',
    ending_price      TEXT    NOT NULL DEFAULT '',
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, symbol)
);
ALTER TABLE investment_transactions ADD COLUMN cleared INTEGER NOT NULL DEFAULT 0;
ALTER TABLE investment_transactions ADD COLUMN reconciled INTEGER NOT NULL DEFAULT 0;
"""


# 66: per-line lock on an allocation target (SRD 5.8f). A target mix must add
# up to 100, so editing one class has to move the others; the lock is how the
# user says "this one is settled, take it out of the adjustable pool". It lives
# on the line rather than in the UI because the decision outlives the dialog.
_V66 = """
ALTER TABLE allocation_target_lines ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;
"""


# 67: instrument kind and option terms on securities (SRD 5.8). Mammon has so
# far had exactly one instrument shape -- a thing with a symbol and a share
# count -- and an option contract is not that thing: it has a multiplier, an
# underlying, an expiration and a strike, and it stops existing on a date. This
# migration only makes those facts STORABLE. It classifies nothing, backfills
# nothing and rewrites no identity; every existing row comes out the far side
# with NULL kind and behaves exactly as it did before.
#
# The shape, and why:
#   * `option_right`, NOT `right`. RIGHT became a SQLite keyword in 3.39 (right
#     joins). It still happens to parse as a bare column name in ALTER TABLE,
#     SELECT, ORDER BY and named INSERT, but a keyword as a column name is a
#     latent parse failure waiting in the one context nobody tests, and the
#     prefix costs nothing.
#   * Columns on `securities`, not a parallel `options` table. Every read path
#     already looks a security up by symbol; a second table would mean a second
#     lookup on every price, holdings and valuation path -- exactly the fan-out
#     that made this defect systemic in the first place.
#   * `multiplier` is Decimal-encoded TEXT, matching the existing share-quantity
#     and per-share-price encoding. Never a float, never an int.
#   * NULL `kind` means UNCLASSIFIED, not `equity`. Code must branch on "known
#     option" versus "not known to be an option", so an unclassified row keeps
#     behaving as it does today and nothing changes under the user until they
#     say so.
#   * `kind_source` so a source-stated type, a derived classification and a user
#     decision stay distinguishable forever -- the same three-valued reasoning
#     that `securities.recorded_ticker` already earns its keep with.
_V67 = """
ALTER TABLE securities ADD COLUMN kind TEXT;            -- NULL = unclassified
ALTER TABLE securities ADD COLUMN multiplier TEXT;      -- Decimal TEXT, NULL = 1
ALTER TABLE securities ADD COLUMN underlying TEXT;      -- the root's identity
ALTER TABLE securities ADD COLUMN expiration TEXT;      -- ISO, NULL = perpetual
ALTER TABLE securities ADD COLUMN strike TEXT;          -- Decimal TEXT
ALTER TABLE securities ADD COLUMN option_right TEXT;    -- 'C' | 'P'
ALTER TABLE securities ADD COLUMN kind_source TEXT;     -- 'source'|'derived'|'user'
"""


# 68 -- realized P/L is replayed by the position's sign, and a sale's amount is
# net cash (investments._apply_trade / _proceeds_of). Covering a short used to
# realize nothing and every sale charged its commission twice, and each year-end
# snapshot stored that year's realized total and lots. They are DERIVED, so they
# are dropped rather than corrected: a read without snapshots is a from-inception
# replay under the new rules, and the next rebuild_holdings writes them again.
# The same shape as _V37, which dropped them when lots arrived.
_V68 = """
DELETE FROM holdings_checkpoints;
"""

# 69 -- an opening balance counts from its OPENING DATE (ledger.opening_balance_on),
# as Quicken's balances do; it used to be added to every date, so an account
# opened in 2002 carried its opening balance in 2000. Year-end balance snapshots
# for years that END before an account's opening date were written with the
# opening balance in them. Only those rows are wrong -- a snapshot for the
# opening year or later includes it under both readings -- so only they are
# dropped; a read without a snapshot is the exact full sum.
_V69 = """
DELETE FROM balance_checkpoints
WHERE account_id IN (SELECT id FROM accounts
                     WHERE opening_balance <> 0 AND opening_date IS NOT NULL AND opening_date <> '')
  AND year < (SELECT CAST(substr(opening_date, 1, 4) AS INTEGER) FROM accounts
              WHERE accounts.id = balance_checkpoints.account_id);
"""

# 70 -- a crypto event's TIME OF DAY. Every crypto source states one (a block
# explorer's UnixTimestamp, an exchange's Timestamp) and the importers used to
# keep only the date, so a wallet's same-day events had no order but the order
# they happened to be inserted in. `time` is HH:MM:SS on the same clock as the
# row's `date` (UTC for every current source); NULL means unknown -- a manual
# entry or a row imported before this column existed, which a re-import of the
# same file fills in (SRD 5.8j). Carried on `review_items` too, so a row queued
# for review keeps its time until it is accepted.
_V70 = """
ALTER TABLE crypto_transactions ADD COLUMN time TEXT;
ALTER TABLE review_items ADD COLUMN time TEXT;
"""

# 71 -- a partial index over the stock-split rows alone. Every valuation at a
# past date asks whether a split falls between a price's date and the date being
# valued (investments.latest_price -> _split_events), and without an index that
# question scanned every investment transaction: net worth history took three
# times as long. The WHERE expression is matched TEXTUALLY by SQLite's planner,
# so _split_events must query with exactly this term.
_V71 = """
CREATE INDEX IF NOT EXISTS idx_invtxn_splits ON investment_transactions(symbol)
    WHERE lower(replace(action,' ',''))='stksplit';
"""

# 72 -- "Dividend" and "Cash Dividend", a broker download's words for a cash
# dividend, now count as dividends (investments._DIVIDEND_ACTIONS). The year-end
# holdings snapshots store each position's dividend total, summed without them,
# so they are dropped and the next rebuild_holdings writes them again -- the
# same shape as _V68.
_V72 = """
DELETE FROM holdings_checkpoints;
"""

# 73 -- a fund CONVERSION kept as one holding for its return. A plan that closes
# a fund and moves the money into a new one records it as the old fund sold and
# the new one bought, often at a different share price, so the share count
# changes by a ratio no split could state. The row says, for ONE account, that
# `from_symbol` continues as `to_symbol` from `date`; investments.link_holding
# only writes it when all of the old fund was sold that day and exactly the
# proceeds bought the new one. It changes nothing but how a return is measured:
# prices, charts, holdings and every stored row stay as they are, which is why
# this is not a security_aliases row (an alias pools price history and folds
# holdings into one identity everywhere).
_V73 = """
CREATE TABLE IF NOT EXISTS holding_links (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    from_symbol TEXT NOT NULL,
    to_symbol   TEXT NOT NULL,
    date        TEXT NOT NULL,
    PRIMARY KEY (account_id, from_symbol)
);
"""

# 74 -- a target mix governs ACCOUNTS THE USER CHOOSES, of ONE tax treatment.
# Mixing kinds of money is wrong (401(k) + IRA is not Roth, and neither is a
# non-taxable special-purpose holding), and accounts must be selectable: a 529
# held for a child is not part of the owner's own assets.
# `accounts.tax_treatment` says what kind of money an account holds (NULL until
# the user says); `allocation_target_accounts` is the target's own account list,
# which replaces the sleeve (an enum whose two values both included cash and so
# never explained why the advice changed); `rebalanced_on` is the date the user
# last acted, which is the span each holding's drift is measured over.
_V74 = """
ALTER TABLE accounts ADD COLUMN tax_treatment TEXT;
ALTER TABLE allocation_targets ADD COLUMN rebalanced_on TEXT;
CREATE TABLE IF NOT EXISTS allocation_target_accounts (
    target_id  INTEGER NOT NULL REFERENCES allocation_targets(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (target_id, account_id)
);
"""

# 75 -- a saved REPORT DEFINITION is data the user owns, so it lives in the
# ledger and not in QSettings: it must ride the backups and snapshots, move with
# the .db to another machine, appear in export.py's dump, and cascade when a
# category or an account is deleted. A report is a `report_defs` header (name,
# kind, the stored date range) plus ordered `report_items`, each of one KIND
# (SOSC = signed net of the selected categories, EDAB/SDAB = end/start display
# account balance, HOLDVAL/RGAIN/NETGAIN and COMPUTED arriving in later phases).
# The selections are SEPARATE child tables keyed by INTEGER ID, never by name --
# `ledger.rename_category` keeps the id, so a name-keyed mapping would evaporate
# the moment the user tidied a category name (SRD 5.9c records that failure for
# the report filter bar; a tax report losing its Schedule E mapping is the worst
# version of it). `include_subtree` is stored as a FLAG, not as an expanded id
# list, so a child category added next year is picked up with no edit.
# Securities are the one name-keyed child, because `securities` is keyed by
# `symbol TEXT PRIMARY KEY` and identity across a ticker change is protected by
# `security_aliases` (migration 64) instead. Report item tags are ORDINARY tags:
# there is deliberately no report_tags table, because two tag vocabularies would
# mean two things to type and two things to rename.
_V75 = """
CREATE TABLE IF NOT EXISTS report_defs (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'custom',   -- 'custom' | 'tax'
    definition_id TEXT,                             -- e.g. 'us-1040-2025', or NULL
    range_kind    TEXT NOT NULL DEFAULT 'fixed',    -- 'fixed'|'calendar_year'|'preset'
    range_start   TEXT,                             -- ISO YYYY-MM-DD
    range_end     TEXT,
    range_year    INTEGER,
    range_preset  TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS report_items (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES report_defs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    label       TEXT,
    group_label TEXT,
    kind        TEXT NOT NULL,                      -- SOSC|EDAB|SDAB|HOLDVAL|RGAIN|NETGAIN|COMPUTED
    sign        INTEGER NOT NULL DEFAULT 1,         -- +1 or -1
    tag_enabled INTEGER NOT NULL DEFAULT 0,
    options     TEXT,                               -- small JSON, kind-specific
    expr        TEXT,                               -- COMPUTED only
    txf_refnum  INTEGER,                            -- export metadata, NULL for non-tax
    txf_copy    INTEGER NOT NULL DEFAULT 1,
    txf_format  INTEGER,
    UNIQUE(report_id, name)
);
CREATE INDEX IF NOT EXISTS idx_report_items_report ON report_items(report_id);

CREATE TABLE IF NOT EXISTS report_item_categories (
    item_id         INTEGER NOT NULL REFERENCES report_items(id) ON DELETE CASCADE,
    category_id     INTEGER NOT NULL REFERENCES categories(id)   ON DELETE CASCADE,
    include_subtree INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_report_item_categories_cat
    ON report_item_categories(category_id);

CREATE TABLE IF NOT EXISTS report_item_accounts (
    item_id    INTEGER NOT NULL REFERENCES report_items(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id)     ON DELETE CASCADE,
    PRIMARY KEY (item_id, account_id)
);

CREATE TABLE IF NOT EXISTS report_item_securities (
    item_id INTEGER NOT NULL REFERENCES report_items(id)   ON DELETE CASCADE,
    symbol  TEXT    NOT NULL REFERENCES securities(symbol) ON DELETE CASCADE,
    PRIMARY KEY (item_id, symbol)
);

CREATE TABLE IF NOT EXISTS report_item_refs (
    item_id     INTEGER NOT NULL REFERENCES report_items(id) ON DELETE CASCADE,
    ref_item_id INTEGER NOT NULL REFERENCES report_items(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, ref_item_id)
);
"""


# An ACCOUNT's own asset mixture: the exact analogue of security_mixtures.
#
# accounts.asset_class could hold only ONE class, so "this sleeve is a
# conservative 30/70" was unsayable -- the user had to pick one class and be
# wrong about the rest, or leave it unclassified. An account holding no
# securities at all (a stable-value sleeve, a managed account reported as a
# single balance) had no way to say anything. A security has been able to say
# it since security_mixtures; an account could not.
#
# Same shape on purpose: the normalize/split_value code in mammon.security_mix
# serves both, so the two cannot drift apart.
_V76 = """
CREATE TABLE account_mixtures (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    asset_class TEXT NOT NULL,
    pct         TEXT NOT NULL,             -- Decimal text, percent of the balance
    source      TEXT,
    as_of       TEXT,
    UNIQUE(account_id, asset_class)
);
CREATE INDEX idx_account_mixtures_account ON account_mixtures(account_id);
"""


# A target's weights stated PER FUND inside an account, the way a broker's
# auto-rebalance is actually set up.
#
# allocation_target_lines states a weight per ASSET CLASS, which is not a
# tradeable unit: a blended fund moves three classes at once, so "sell $30,000
# of domestic stock" cannot be executed without decomposing it across holdings
# whose mixes differ. The user's own career practice was the other way round --
# pick funds, give each a target percent, rebalance the funds -- which is
# directly executable, produces the buy-low/sell-high effect by construction,
# and restores the class mix as a CONSEQUENCE.
#
# So a target may state its weights in funds instead, and the class mix becomes
# a computed read-out to check against rather than an instruction to follow.
# Percent is of the ACCOUNT, because an account is the unit you can trade
# within: money does not move between a 401(k) and a taxable account.
_V77 = """
CREATE TABLE allocation_target_funds (
    id         INTEGER PRIMARY KEY,
    target_id  INTEGER NOT NULL REFERENCES allocation_targets(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol     TEXT NOT NULL,
    pct        TEXT NOT NULL,             -- Decimal text, percent of the ACCOUNT
    UNIQUE(target_id, account_id, symbol)
);
CREATE INDEX idx_allocation_target_funds_target
    ON allocation_target_funds(target_id);
"""


# The retirement planner's storage: who the household is, and the per-year
# withdrawal/conversion plan. See mammon/retirement.py for the math over it.
#
# `people` exists because ten of the planner's figures need an AGE and the
# schema had no person anywhere -- accounts carry no owner and no birthdate.
# Birth MONTH and YEAR, not a full date, because every rule Mammon computes
# keys off a month, a year, or an attained whole-numbered age: Medicare
# entitlement begins on the first day of a month (42 CFR 406.10), the Social
# Security reduction counts months (42 U.S.C. 402(q)), the RMD applicable age
# keys off birth year alone (IRC 401(a)(9)(C)(v)), and mortality tables are
# indexed by single year of age. The day never enters -- with ONE exception:
# SSA and CMS follow the common-law rule that a person attains an age the day
# BEFORE their birthday anniversary, so someone born on the 1st attains it in
# the PRIOR month. `born_on_the_first` is the only thing a full date would have
# told us, so it is a flag rather than a reason to demand a birthday from every
# person in the file.
#
# The health columns are optional, blank by default, never imported and never
# asked for at onboarding. They are here because a Mammon ledger is a local
# file its owner controls -- never uploaded, never synced -- and a plan built
# around living to 95 is the wrong plan for a diabetic smoker. The guard is
# technical, not a prohibition: mammon/mcp_tools.py DENIES every one of these
# columns at the MCP boundary (see retirement.HEALTH_COLUMNS), and
# test_mcp_tools.py fails if a new one is added without being denied.
#
# current_employer_plan is a SEPARATE column, never an overload of
# tax_treatment, whose `deferred` value covers a current 401(k), a former
# 401(k) and a traditional IRA alike and has settled meanings in rebalance.py
# and capital_gains.py that must not be disturbed. Two rules turn on the
# distinction: the still-working exception to RMDs (IRC 401(a)(9)(C)(i)(II),
# which does not apply to IRAs or to a former employer's plan) and the rule of
# 55. The UPDATE below only SEEDS a default from "has an inflow in the trailing
# twelve months"; the stored flag is authoritative from then on, because a
# person on sabbatical, between jobs, or still contributing to a plan they have
# left would all be misclassified silently by the inference.
#
# The two plan tables are NOT transactions and never become any: mammon.ledger
# stays the only writer of transaction rows. These hold an intention about a
# future year, which no register row can express.
_V78 = """
CREATE TABLE people (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    relationship      TEXT NOT NULL DEFAULT 'other',  -- self|spouse|child|other
    birth_month       INTEGER,                        -- 1-12, NULL when unknown
    birth_year        INTEGER,                        -- NULL when unknown
    born_on_the_first INTEGER NOT NULL DEFAULT 0,
    smoker            TEXT,                           -- health: denied at the MCP boundary
    bmi_band          TEXT,                           -- health: denied at the MCP boundary
    diabetes          TEXT,                           -- health: denied at the MCP boundary
    major_conditions  TEXT,                           -- health: denied at the MCP boundary
    family_history    TEXT,                           -- health: denied at the MCP boundary
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (relationship IN ('self', 'spouse', 'child', 'other')),
    CHECK (birth_month IS NULL OR (birth_month BETWEEN 1 AND 12)),
    CHECK (born_on_the_first IN (0, 1))
);

ALTER TABLE accounts ADD COLUMN current_employer_plan INTEGER NOT NULL DEFAULT 0;

UPDATE accounts SET current_employer_plan = 1
 WHERE LOWER(COALESCE(tax_treatment, '')) = 'deferred'
   AND EXISTS (SELECT 1 FROM transactions t
                WHERE t.account_id = accounts.id
                  AND t.amount > 0
                  AND t.date >= date('now', '-12 months'));

CREATE TABLE retirement_withdrawals (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    year         INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,   -- magnitude drawn OUT; see retirement.py
    UNIQUE(account_id, year)
);
CREATE INDEX idx_retirement_withdrawals_year ON retirement_withdrawals(year);

CREATE TABLE retirement_conversions (
    id              INTEGER PRIMARY KEY,
    from_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    to_account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    year            INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL DEFAULT 0,  -- magnitude moved; see retirement.py
    UNIQUE(from_account_id, to_account_id, year)
);
CREATE INDEX idx_retirement_conversions_year ON retirement_conversions(year);
"""


# v79: a person's Social Security earnings record, one row per year, plus the
# two facts the claiming table needs from the person themself.
#
# Why a table of years rather than one typed PIA. The design document's first
# pass had the user transcribe a PIA off their SSA statement and stop there,
# on the reasoning that reconstructing a 35-year indexed earnings record from
# payroll deposits would be wrong and confidently so. The user then asked for
# the earnings report itself - entered year by year, or estimated from ledger
# data - because a PIA on a statement is quoted for ONE claim age and goes
# stale the moment another year of earnings lands. Both shapes survive here:
# ``source`` records, per year, which one produced the figure, so no screen
# ever has to guess whether a number came from SSA or from Mammon.
#
#   reported  - typed off the SSA Earnings Report. Authoritative, already
#               capped at that year's taxable maximum by SSA.
#   estimated - summed from wage-category inflows in this ledger. A guess with
#               a stated method; capped at the taxable maximum on read.
#   projected - a year the user expects to earn, after the report's last year.
#
# The magnitude is unsigned: an earnings record has no outflows, and storing
# it signed would invite a negative year that silently lowers an AIME.
_V79 = """
CREATE TABLE person_earnings (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    year           INTEGER NOT NULL,
    earnings_cents INTEGER NOT NULL DEFAULT 0,   -- magnitude, never signed
    source         TEXT NOT NULL DEFAULT 'reported',
    UNIQUE(person_id, year),
    CHECK (earnings_cents >= 0),
    CHECK (source IN ('reported', 'estimated', 'projected'))
);
CREATE INDEX idx_person_earnings_person ON person_earnings(person_id, year);

ALTER TABLE people ADD COLUMN planned_claim_age_months INTEGER;
ALTER TABLE people ADD COLUMN ss_statement_date TEXT;
"""


# v80: who owns a retirement account, and the projected taxable income series
# the Roth section on the planner page draws and edits.
#
# Why an owner column. A Roth conversion has no spousal form: IRC 408A(d)(3)
# rolls a distribution from an IRA or employer plan into a Roth owned by the
# SAME individual, so "convert one spouse's IRA into the other's Roth" is not a transaction
# that exists. Until now nothing in the schema could tell those two Roths
# apart, which meant the planner could offer - and store - a conversion no
# custodian would process. ``owner_person_id`` is nullable on purpose: an
# existing ledger has no owners recorded, and an account whose owner is unknown
# stays usable everywhere rather than being locked out of a plan. The rule is
# therefore enforced as "two KNOWN owners must match", not "an owner is
# required".
#
# Why taxable income is stored rather than computed. The plan's own flows can
# seed it (the taxable part of Social Security plus tax-deferred distributions)
# but they cannot know about wages, a pension, capital gains or a spouse's
# consulting year, and the whole point of the bracket lines is comparing a
# YEAR'S income against a bracket edge. So the series is the user's to edit, one
# unsigned row per year. It holds BASE income only - planned conversions are
# stacked on top when the bar is drawn - because a stored total that already
# included the conversion would double count it the moment the user clicked a
# bracket line to set one.
_V80 = """
ALTER TABLE accounts ADD COLUMN owner_person_id INTEGER REFERENCES people(id);

CREATE TABLE retirement_taxable_income (
    id           INTEGER PRIMARY KEY,
    year         INTEGER NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL DEFAULT 0,  -- magnitude, base income, no conversions
    source       TEXT NOT NULL DEFAULT 'entered',
    CHECK (amount_cents >= 0),
    CHECK (source IN ('entered', 'seeded'))
);
"""


# _V81: the household withdrawal plan - one starting amount and one annual
# increase for the whole retirement pool, not a figure per account.
#
# A single row, because the household has exactly one of these. The per-account,
# per-year amounts it produces still live in retirement_withdrawals; this table
# holds only the two numbers a person typed, so reopening the page shows the
# plan they set rather than an empty box over a grid of derived figures. The
# increase is Decimal-encoded TEXT for the same reason share quantities are:
# "2.5" percent through forty years compounds, and a float would drift.
_V81 = """
CREATE TABLE retirement_withdrawal_plan (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    start_cents  INTEGER NOT NULL DEFAULT 0,   -- magnitude, the FIRST planned year
    increase_pct TEXT NOT NULL DEFAULT '0',    -- percent per year, Decimal as TEXT
    CHECK (start_cents >= 0)
);
"""


# _V82: PLANNED (hypothetical) Roth accounts, which exist only in the plan.
#
# A conversion is done at the custodian that holds the money: convert an IRA
# and the money lands in a Roth at the same custodian, which it opens if there
# is none. So the planner sends a conversion to a Roth of the SAME owner at the SAME
# institution, and when the ledger has no such account it plans into a
# hypothetical one. Before this, the only targets were the Roths already in the
# ledger, and with no owners recorded a 401(k) at one custodian was planned
# into a spouse's Roth at another.
#
# The hypothetical account lives here rather than as an accounts row because it
# holds no money and no transactions; an account row would show up in every
# account list, balance and report that reads that table. Withdrawals and
# conversions therefore name EITHER a real account or a planned one, exactly one
# of the two (the CHECKs). The UNIQUE constraints become partial indexes because
# a UNIQUE over a nullable column treats every NULL as distinct. The unique
# (owner, institution) index COALESCEs the owner for the same reason: two
# planned Roths for "owner unknown at one custodian" would be two targets for one
# conversion. Both plan tables are rebuilt; no other table references them.
_V82 = """
CREATE TABLE retirement_planned_accounts (
    id              INTEGER PRIMARY KEY,
    owner_person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
    institution     TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX ux_retirement_planned_owner_institution
    ON retirement_planned_accounts(COALESCE(owner_person_id, 0), institution);

CREATE TABLE retirement_withdrawals_v82 (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    planned_id   INTEGER REFERENCES retirement_planned_accounts(id) ON DELETE CASCADE,
    year         INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,   -- magnitude drawn OUT; see retirement.py
    CHECK ((account_id IS NULL) <> (planned_id IS NULL))
);
INSERT INTO retirement_withdrawals_v82 (id, account_id, year, amount_cents)
    SELECT id, account_id, year, amount_cents FROM retirement_withdrawals;
DROP TABLE retirement_withdrawals;
ALTER TABLE retirement_withdrawals_v82 RENAME TO retirement_withdrawals;
CREATE UNIQUE INDEX ux_retirement_withdrawals_account
    ON retirement_withdrawals(account_id, year) WHERE account_id IS NOT NULL;
CREATE UNIQUE INDEX ux_retirement_withdrawals_planned
    ON retirement_withdrawals(planned_id, year) WHERE planned_id IS NOT NULL;
CREATE INDEX idx_retirement_withdrawals_year ON retirement_withdrawals(year);

CREATE TABLE retirement_conversions_v82 (
    id              INTEGER PRIMARY KEY,
    from_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    to_account_id   INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    to_planned_id   INTEGER REFERENCES retirement_planned_accounts(id) ON DELETE CASCADE,
    year            INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL DEFAULT 0,  -- magnitude moved; see retirement.py
    CHECK ((to_account_id IS NULL) <> (to_planned_id IS NULL))
);
INSERT INTO retirement_conversions_v82
    (id, from_account_id, to_account_id, year, amount_cents)
    SELECT id, from_account_id, to_account_id, year, amount_cents
      FROM retirement_conversions;
DROP TABLE retirement_conversions;
ALTER TABLE retirement_conversions_v82 RENAME TO retirement_conversions;
CREATE UNIQUE INDEX ux_retirement_conversions_account
    ON retirement_conversions(from_account_id, to_account_id, year)
    WHERE to_account_id IS NOT NULL;
CREATE UNIQUE INDEX ux_retirement_conversions_planned
    ON retirement_conversions(from_account_id, to_planned_id, year)
    WHERE to_planned_id IS NOT NULL;
"""


# _V83: other income, and when and how the household plan draws.
#
# Social Security and account draws were the only income the planner knew, so a
# household with rentals or royalties saw the plan draw money it would never
# need, and its taxable income - which decides the bracket room for Roth
# conversions and for IRA draws - was understated. A source is an annual amount
# from a start year, changing by a percentage each year (0 for flat rent,
# negative for royalties that fall off), with an optional end year; Decimal TEXT
# for the percentage, as everywhere else. ``taxable`` is a flag rather than
# assumed, because a gift or a Roth-like source is income the bracket must not
# see.
#
# The household plan gains a start year (retire before the first draw, or draw
# before Social Security) and an optional bracket rate: fill tax-deferred draws
# only up to the top of that bracket and take the rest from Roth.
_V83 = """
CREATE TABLE retirement_income_sources (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,   -- per year, in the start year
    start_year   INTEGER NOT NULL,
    end_year     INTEGER,                      -- NULL: continues
    change_pct   TEXT NOT NULL DEFAULT '0',    -- percent per year, Decimal as TEXT
    taxable      INTEGER NOT NULL DEFAULT 1,
    CHECK (amount_cents >= 0),
    CHECK (taxable IN (0, 1)),
    CHECK (end_year IS NULL OR end_year >= start_year)
);

ALTER TABLE retirement_withdrawal_plan ADD COLUMN start_year INTEGER;
ALTER TABLE retirement_withdrawal_plan ADD COLUMN bracket_rate TEXT;
"""


# _V84: the planner's household assumptions - for now, the Social Security COLA.
#
# Benefits were projected in today's dollars while the spending need rose every
# year, so the plan leaned harder on the accounts each year than a real
# household would. One row, a Decimal TEXT percentage; no row means the code's
# default (``retirement.DEFAULT_COLA_PCT``).
_V84 = """
CREATE TABLE retirement_assumptions (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    cola_pct TEXT NOT NULL DEFAULT '2.4'     -- percent per year, Decimal as TEXT
);
"""


# _V85: bracket indexing. The bracket tops and the standard deduction are
# indexed to inflation every year (IRC 1(f), 63(c)(4); chained CPI-U), so a
# plan held at the 2026 tables for forty years showed the household squeezed
# into higher brackets it would never reach. A separate rate from the COLA
# because the indexes differ: chained CPI-U runs below the CPI-W that COLAs use.
_V85 = """
ALTER TABLE retirement_assumptions
    ADD COLUMN bracket_index_pct TEXT NOT NULL DEFAULT '2.2';
"""


# _V86: Social Security's trust-fund shortfall. The Trustees project the OASI
# trust fund depleted in 2033, after which continuing payroll taxes pay about
# 77% of scheduled benefits. A plan that assumed full benefits forever was a
# plan on Social Security "always being there" (reported). Both numbers are
# editable assumptions; 100 turns the cut off.
_V86 = """
ALTER TABLE retirement_assumptions
    ADD COLUMN ss_shortfall_year INTEGER NOT NULL DEFAULT 2033;
ALTER TABLE retirement_assumptions
    ADD COLUMN ss_payable_pct TEXT NOT NULL DEFAULT '77';
"""


# _V87: giving - a tithe or any regular charitable gift - and itemizing.
#
# A household that gives a tenth of its income itemizes, and pays for the gift
# out of the same accounts the plan draws. From 70 1/2 the gift can go straight
# from an IRA as a qualified charitable distribution: never taxable income,
# and counted toward the year's required minimum (IRC 408(d)(8)). Before that,
# giving two years' worth every other year ("bunching") itemizes one year and
# takes the standard deduction the next. One row: a percentage of income OR an
# amount rising by a percentage, whether to bunch, and the household's other
# itemized deductions (state and property tax, mortgage interest).
_V87 = """
CREATE TABLE retirement_giving (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    pct                  TEXT NOT NULL DEFAULT '0',     -- percent of income
    amount_cents         INTEGER NOT NULL DEFAULT 0,    -- or a fixed amount
    increase_pct         TEXT NOT NULL DEFAULT '0',     -- the amount's yearly rise
    bunched              INTEGER NOT NULL DEFAULT 0,
    other_itemized_cents INTEGER NOT NULL DEFAULT 0,
    CHECK (amount_cents >= 0),
    CHECK (other_itemized_cents >= 0),
    CHECK (bunched IN (0, 1))
);
"""


# _V88: what a percentage gift is figured on. Households differ: one tithes on
# gross pay (less 401(k) deferrals), so it tithes on IRA/401(k) money when it
# comes OUT, not on Roth withdrawals, and on Social Security only once benefits
# exceed the Social Security tax it paid in (reported). Each income kind is a
# switch; Social Security is 'all', 'excess' (beyond the tax paid in) or 'none'.
_V88 = """
ALTER TABLE retirement_giving ADD COLUMN on_other INTEGER NOT NULL DEFAULT 1;
ALTER TABLE retirement_giving ADD COLUMN on_ira INTEGER NOT NULL DEFAULT 1;
ALTER TABLE retirement_giving ADD COLUMN on_conversions INTEGER NOT NULL DEFAULT 1;
ALTER TABLE retirement_giving ADD COLUMN on_roth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE retirement_giving ADD COLUMN on_social_security TEXT NOT NULL
    DEFAULT 'excess' CHECK (on_social_security IN ('all', 'excess', 'none'));
"""


# _V89: giving is not modeled after all. v87/v88 planned a tithe - a percentage
# of chosen income kinds, bunching, itemizing, QCDs from 70 1/2 - inside the
# retirement plan. Projecting itemized deductions and gift timing thirty years
# out claimed a precision no household plans with, so the household includes
# its giving in the spending amount and taxable income takes the standard
# deduction. The table those migrations created is dropped rather than left as
# dead schema; the migrations themselves stay, as every applied one must.
_V89 = """
DROP TABLE IF EXISTS retirement_giving;
"""


# _V90: a salary's 401(k) deferral and employer match. With the percentages and
# the account they go into, the planner computes that account's contributions
# from the salary (and its raises) instead of a measured average, and the
# deferral - pre-tax unless the account is a Roth - comes out of taxable income
# and out of the money that pays the household's spending. Reported: the
# planner could, if it knew the deferral percentage and the employer match.
_V90 = """
ALTER TABLE retirement_income_sources ADD COLUMN deferral_pct TEXT NOT NULL DEFAULT '0';
ALTER TABLE retirement_income_sources ADD COLUMN match_pct TEXT NOT NULL DEFAULT '0';
ALTER TABLE retirement_income_sources ADD COLUMN into_account_id INTEGER
    REFERENCES accounts(id) ON DELETE SET NULL;
"""


# _V91: a salary is a different kind of income from rent or royalties. It has a
# 401(k) deferral, a match and an account they go into; other income has none
# of that. Reported: one table holding both "tried to fit disparate things on
# the same row". The Income dialog shows the salary as its own form and other
# income as the table, so each source says which it is. An existing source that
# already carries a 401(k) link, or is named like a salary, becomes the salary.
_V91 = """
ALTER TABLE retirement_income_sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'other'
    CHECK (kind IN ('salary', 'other'));
UPDATE retirement_income_sources SET kind = 'salary'
 WHERE deferral_pct <> '0' OR match_pct <> '0' OR into_account_id IS NOT NULL
    OR LOWER(name) LIKE 'salary%' OR LOWER(name) LIKE 'wage%';
"""


# _V92: whose salary it is. A household can have two earners, each retiring on
# their own schedule (reported: a second earner retires on their own), so a salary
# names its person: the Income dialog shows one salary section per earner, and
# a salary's through year - not the household's - ends its contributions and
# turns the plan it funds into a former employer's. An existing salary belongs
# to the self person.
_V92 = """
ALTER TABLE retirement_income_sources ADD COLUMN person_id INTEGER
    REFERENCES people(id) ON DELETE SET NULL;
UPDATE retirement_income_sources
   SET person_id = (SELECT id FROM people WHERE relationship = 'self'
                     ORDER BY id LIMIT 1)
 WHERE kind = 'salary';
"""


# _V93: pensions and investment income are their own kinds of income. The
# Income dialog gives each a section (reported: "we are missing interest and
# dividends from investments in non-retirement accounts ... we are missing
# pension income"): a pension per earner, and one investment-income line for
# the household. SQLite cannot widen a CHECK in place, so the table is rebuilt
# with every column copied; nothing references it by foreign key.
_V93 = """
CREATE TABLE retirement_income_sources_v93 (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    start_year      INTEGER NOT NULL,
    end_year        INTEGER,
    change_pct      TEXT NOT NULL DEFAULT '0',
    taxable         INTEGER NOT NULL DEFAULT 1,
    deferral_pct    TEXT NOT NULL DEFAULT '0',
    match_pct       TEXT NOT NULL DEFAULT '0',
    into_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL DEFAULT 'other'
        CHECK (kind IN ('salary', 'pension', 'investment', 'other')),
    person_id       INTEGER REFERENCES people(id) ON DELETE SET NULL,
    CHECK (amount_cents >= 0),
    CHECK (taxable IN (0, 1)),
    CHECK (end_year IS NULL OR end_year >= start_year)
);
INSERT INTO retirement_income_sources_v93
    (id, name, amount_cents, start_year, end_year, change_pct, taxable,
     deferral_pct, match_pct, into_account_id, kind, person_id)
SELECT id, name, amount_cents, start_year, end_year, change_pct, taxable,
       deferral_pct, match_pct, into_account_id, kind, person_id
  FROM retirement_income_sources;
DROP TABLE retirement_income_sources;
ALTER TABLE retirement_income_sources_v93 RENAME TO retirement_income_sources;
"""


# _V94: the mix the Retirement Planner projects at, when it is not the one held
# today. The Investment Center's What If mix is never stored (it is a what-if),
# so "Use What If Mix" COPIES it here (reported: the projection used the
# current, aggressive mix, not the one the household plans to hold). NULL means
# measure each account's actual holdings, as before.
_V94 = """
ALTER TABLE retirement_assumptions ADD COLUMN planning_risk TEXT;
"""


# _V95: the tax rate on whatever is left in the tax-deferred accounts at the
# plan's end. The plan's tax total stops at its last year; what remains in the
# IRAs is taxed later - to the household past that age or to heirs - and
# leaving it out made conversions, which shrink that bill, look worse than they
# are (reported). An assumption, default 24%.
_V95 = """
ALTER TABLE retirement_assumptions ADD COLUMN end_ira_tax_pct TEXT NOT NULL DEFAULT '24';
"""


# _V96: the household's spending order and the accounts it will not draw from.
# The order the plan spends in was fixed (IRA room, taxable, Roth, IRA over the
# bracket); households differ - one pays conversion tax from savings, another
# will not sell appreciated stock in a year the gain would be taxed - so the
# order is theirs to set (reported). NULL is the default order. An exempt
# account is never drawn for spending or tax; a tax-deferred one still pays its
# required minimum, which the law does not let anyone waive.
_V96 = """
ALTER TABLE retirement_assumptions ADD COLUMN spending_order TEXT;
CREATE TABLE retirement_spending_exempt (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE
);
"""


# _V97: how fast Medicare's premiums - and so every IRMAA surcharge, which is
# a share of the same per-enrollee cost - grow. The income tiers are indexed to
# inflation; the dollars grew about 5% a year 2016-2026, well above it, so the
# planner's IRMAA needs its own rate. An assumption, default 5%.
_V97 = """
ALTER TABLE retirement_assumptions ADD COLUMN medicare_growth_pct TEXT NOT NULL DEFAULT '5';
"""


# _V98: the survivor scenario. One spouse dies at the end of a chosen year and
# the plan runs on for the other: single filing, the larger of the two Social
# Security benefits, the deceased's salary gone and a pension cut to its
# survivor share, the IRAs rolled to the survivor, one Medicare enrollee, and
# spending scaled down. Reported: "add the survivor scenario". NULL person =
# off; the two percentages are assumptions (75% of spending, a 50% pension -
# the least a married participant's joint-and-survivor annuity can pay).
_V98 = """
ALTER TABLE retirement_assumptions ADD COLUMN survivor_person_id INTEGER;
ALTER TABLE retirement_assumptions ADD COLUMN survivor_death_year INTEGER;
ALTER TABLE retirement_assumptions ADD COLUMN survivor_spending_pct TEXT NOT NULL DEFAULT '75';
ALTER TABLE retirement_assumptions ADD COLUMN pension_survivor_pct TEXT NOT NULL DEFAULT '50';
"""


# _V99: assume the IRMAA appeal after a life-changing event (Form SSA-44). A
# work stoppage - retiring - or a spouse's death lets the household ask Social
# Security to set the surcharge from the premium year's own income instead of
# the year two back (reported: "you can get an exemption for life events like
# retirement"). On by default: a household that retires files it.
_V99 = """
ALTER TABLE retirement_assumptions ADD COLUMN ssa44_appeal INTEGER NOT NULL DEFAULT 1;
"""


# _V100: the Retirement Plan's asset mix, typed as stocks/bonds/cash
# percentages ("60,35,5"). It replaces copying the Investment Center's What If
# level (reported: the mix should be set explicitly in the plan, not be
# whatever What If said at some point). planning_risk stays the level the
# projections consume, derived from this mix.
_V100 = """
ALTER TABLE retirement_assumptions ADD COLUMN plan_mix TEXT;
"""


# _V101: the planner's "Plan through age", kept with the ledger. It was a
# spin box that reset to 90 every session, and the Investment Center now draws
# the whole plan's horizon from it (reported: "make the default horizon that
# which includes the whole plan").
_V101 = """
ALTER TABLE retirement_assumptions ADD COLUMN plan_through_age INTEGER NOT NULL DEFAULT 90;
"""


# _V102: where the income tax is paid from, and which other income is net
# investment income. Paid from the IRA, the tax used bracket room a conversion
# could have had (reported: an option to pay it from a taxable account);
# paid from a taxable account it realizes gains, which the plan now
# taxes at the capital-gains rates. The 3.8% net investment income tax (IRC
# 1411) needs to know which income is investment income: rent usually is, so a
# source named like rent starts marked.
_V102 = """
ALTER TABLE retirement_assumptions ADD COLUMN tax_paid_from TEXT NOT NULL DEFAULT 'spending';
ALTER TABLE retirement_income_sources ADD COLUMN niit INTEGER NOT NULL DEFAULT 0;
UPDATE retirement_income_sources SET niit = 1
 WHERE kind = 'other' AND lower(name) LIKE '%rent%';
"""


# _V103: the lots a coin transfer carried, on its incoming leg - each one's
# original acquisition date, quantity and cost, as JSON. Coin moved between the
# user's own accounts keeps its holding period (IRC 1223); dated on the transfer
# day, coin mined years earlier read as bought the day it reached an exchange,
# and any sale within a year of that as short-term (reported).
_V103 = """
ALTER TABLE crypto_transactions ADD COLUMN lots TEXT;
"""


# _V104: state income tax and the ACA premium credit. The plan was federal-only;
# for most households a state tax is the largest cost it left out, and it
# changes the conversion math. A flat rate on federal AGI, Social Security
# excluded unless the state taxes it (most do not). Retiring before 65 means
# marketplace coverage, whose premium credit is lost ENTIRELY a dollar over 400%
# of the poverty line: the household states the credit it has at stake.
_V104 = """
ALTER TABLE retirement_assumptions ADD COLUMN state_tax_pct TEXT NOT NULL DEFAULT '0';
ALTER TABLE retirement_assumptions ADD COLUMN state_taxes_ss INTEGER NOT NULL DEFAULT 0;
ALTER TABLE retirement_assumptions ADD COLUMN aca_credit_cents INTEGER NOT NULL DEFAULT 0;
"""

# _V105: two figures the plan cannot read from itself. IRMAA in the plan's first
# two years is set by income from BEFORE the plan - the household's own returns,
# which the ledger cannot reconstruct - so those years' modified AGI is typed,
# by year. And the marketplace premium credit before 65 is a slope and then a
# cliff, not a cliff alone: the credit is the benchmark premium less a share of
# income (IRC 36B(b)(3)(A)), so the household states the benchmark premium and
# the plan computes the credit each year; ``aca_credit_cents`` (_V104) is no
# longer read.
_V105 = """
CREATE TABLE retirement_prior_income (
    year INTEGER PRIMARY KEY,
    magi_cents INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE retirement_assumptions ADD COLUMN aca_benchmark_cents INTEGER NOT NULL DEFAULT 0;
"""

# _V106: what an audit of the Retirement Planner's rules found the ledger could
# not say. On an account: that it is an IRA (whose minimums may be totaled and
# taken from any IRA, Reg. 1.408-8, and which nothing else is); that it was
# inherited from someone other than a spouse - the year of death and whether the
# decedent had begun required distributions, which together pick the ten-year
# rule's shape (IRC 401(a)(9)(H)); that its owner owns 5% of the employer (no
# still-working exception, IRC 416(i)); and its after-tax basis (nondeductible
# contributions, Form 8606). On a person: Part D drug coverage, without which
# no Part D surcharge is owed. On an income source: that it is taxed at the
# capital-gains rates (qualified dividends, IRC 1(h)(11)). Three household
# choices: taking the first required minimum by the following April 1, a
# community-property state's full step-up at death, and a state that exempts
# retirement income.
_V106 = """
ALTER TABLE accounts ADD COLUMN is_ira INTEGER NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN inherited_death_year INTEGER;
ALTER TABLE accounts ADD COLUMN inherited_after_rbd INTEGER NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN five_percent_owner INTEGER NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN after_tax_basis_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE people ADD COLUMN part_d INTEGER NOT NULL DEFAULT 1;
ALTER TABLE retirement_income_sources ADD COLUMN preferential INTEGER NOT NULL DEFAULT 0;
ALTER TABLE retirement_assumptions ADD COLUMN defer_first_rmd INTEGER NOT NULL DEFAULT 0;
ALTER TABLE retirement_assumptions ADD COLUMN community_property INTEGER NOT NULL DEFAULT 0;
ALTER TABLE retirement_assumptions ADD COLUMN state_excludes_retirement INTEGER NOT NULL DEFAULT 0;
"""

# _V107: the Budget Planner's Set tab (SRD 5.12b). ``budget_lines`` already has
# the right grain -- one target per (budget, category, period) -- and is NOT
# changed here; what it has no room for is the per-category *intent* behind a
# line: which of the three buckets the category is (fixed | flex | nonmonthly),
# whether and how its remainder rolls forward, and, for a non-monthly expense,
# the yearly total the twelve monthly lines are spread from.
#
# That state hangs off a side table keyed by (budget_id, category_id) rather
# than off ``categories``, for two reasons. There is no ALTER TABLE categories
# anywhere in this file and this is not the place to add the first one; and the
# state is per-BUDGET by nature -- two scenarios may bucket the same category
# differently, which a column on the category could not express.
#
# ``budget_carry_overrides`` is the escape hatch for a carry that must simply
# start again: rollover is computed from the lines and the ledger, never stored,
# so the only way to say "reset this envelope to zero in March" is an explicit,
# dated, note-carrying row that terminates the recursion. Storing the override
# (and only the override) is what keeps the computed carry from drifting.
#
# The three columns on ``budgets`` are the frame that recursion needs:
# ``start_period`` is the defined first month without which the carry walks
# backward forever, ``end_period`` bounds a closed scenario (NULL = open-ended),
# and ``note`` is what a scenario is FOR ("what if the mortgage were gone").
# All three are nullable, so every existing budget row stays valid as it is.
_V107 = """
CREATE TABLE budget_category_settings (
    budget_id     INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    bucket        TEXT NOT NULL DEFAULT 'flex',   -- fixed|flex|nonmonthly
    rollover_mode TEXT NOT NULL DEFAULT 'none',   -- none|positive|both
    annual_cents  INTEGER NOT NULL DEFAULT 0,     -- nonmonthly: the yearly total
    group_id      INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    PRIMARY KEY (budget_id, category_id)
);
CREATE TABLE budget_carry_overrides (
    budget_id    INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    period       TEXT NOT NULL,                  -- ISO 'YYYY-MM'
    amount_cents INTEGER NOT NULL,               -- the carry-in, as set by hand
    note         TEXT,
    set_at       TEXT NOT NULL,                  -- ISO date, for the audit trail
    PRIMARY KEY (budget_id, category_id, period)
);
ALTER TABLE budgets ADD COLUMN start_period TEXT;
ALTER TABLE budgets ADD COLUMN end_period TEXT;
ALTER TABLE budgets ADD COLUMN note TEXT;
"""

# _V108: saving and debt pay-down targets for the Budget Planner (SRD 5.12d).
# Money moved from a cash-flow account (checking, credit, cash) into any other
# account -- a 401(k) deferral split off a paycheck, a mortgage's principal leg,
# a transfer to savings -- is not spending, but it is what a household plans
# around, so a budget targets it per DESTINATION ACCOUNT and per month.
#
# It is a table of its own rather than a nullable ``account_id`` on
# ``budget_lines`` because that table's ``category_id`` is NOT NULL and its
# UNIQUE key is the upsert target every category writer relies on; loosening
# both would need a table rebuild and would teach every category reader to skip
# rows that are not categories. A saving target has no bucket, no rollover and
# no carry (a month that saved more did not "overspend" the next), so it needs
# none of the side tables either. Accounts are closed, never deleted, but the
# key cascades anyway so a deleted account could not leave a target behind.
_V108 = """
CREATE TABLE budget_saving_lines (
    budget_id    INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period       TEXT NOT NULL,                  -- ISO 'YYYY-MM'
    amount_cents INTEGER NOT NULL,               -- net cents to move in that month
    PRIMARY KEY (budget_id, account_id, period)
);
"""

# _V109: savings goals for the Budget Planner (SRD 5.12g). A goal is a named
# target with an optional deadline, and it is the first storage of its kind in
# the schema -- nothing here tracked "saving toward something" before.
#
# Two funding modes, because households keep savings both ways. ACCOUNT-BACKED
# (``account_id`` set) reads the account's own balance less ``baseline_cents``,
# which is the part of that balance the goal does not get to claim. ALLOCATED
# (``account_id`` NULL) sums ``savings_goal_allocations``, rows that ANNOTATE an
# existing transaction and never create, edit or delete one -- so "mammon.ledger
# is the only writer of transaction rows" holds literally. ON DELETE CASCADE on
# ``txn_id`` means a deleted or re-imported transaction cannot strand a claim.
#
# The partial unique index is arithmetic, not taste: two live goals sharing one
# account would each read the same balance, each claim the same dollars, and the
# two progress bars would sum to 200 percent of the money. One account, one live
# goal; a household wanting three goals in one account uses the allocated mode.
# Archived goals are excluded from the index so last year's finished goal does
# not block this year's on the same account.
_V109 = """
CREATE TABLE savings_goals (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    target_cents       INTEGER NOT NULL,            -- positive magnitude
    target_date        TEXT,                        -- ISO 'YYYY-MM-DD', NULL: no deadline
    account_id         INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    baseline_cents     INTEGER NOT NULL DEFAULT 0,  -- balance NOT credited to the goal
    baseline_date      TEXT,                        -- ISO, when the baseline was taken
    budget_id          INTEGER REFERENCES budgets(id) ON DELETE SET NULL,
    category_id        INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    monthly_cents      INTEGER NOT NULL DEFAULT 0,  -- the planned contribution
    priority           INTEGER NOT NULL DEFAULT 0,
    note               TEXT,
    archived           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL                -- ISO date
);
CREATE UNIQUE INDEX idx_savings_goals_account ON savings_goals(account_id)
    WHERE account_id IS NOT NULL AND archived = 0;
CREATE TABLE savings_goal_allocations (
    goal_id      INTEGER NOT NULL REFERENCES savings_goals(id) ON DELETE CASCADE,
    txn_id       INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (goal_id, txn_id)
);
"""

# _V110: revolving-debt terms and payoff plans (SRD 5.12h, and the per-account
# credit-card state of SRD 3.x). This is INPUT storage only, and the three
# tables divide along that line: what the card's agreement says
# (``debt_terms``), which debts the user is attacking together and with how much
# money (``debt_plans``, ``debt_plan_members``).
#
# ``accounts`` is deliberately NOT altered. An APR, a credit limit and a
# minimum-payment form describe a lending agreement, not an account, and half
# the ledger's accounts would carry the columns as dead NULLs. A side table also
# lets a user record terms for a card and later delete them without touching the
# account row the register depends on.
#
# Rates are Decimal-encoded TEXT, like every other rate in this schema
# (``loan_rates``, price history) -- never floats, which cannot hold 19.99
# exactly, and never basis points, which silently truncate a 12.375 percent
# promo. ``min_form`` is 'A' (flat floor), 'B' (max of floor and a percent of
# balance) or 'C' (max of floor and percent plus that month's interest), the
# three forms real issuers use; 'C' with a 25.00 floor and 1 percent is the
# default because it is the commonest and the most pessimistic of the three.
# ``estimated`` exists so the page can SAY the terms are a guess rather than
# quietly presenting a synthesized minimum as the issuer's.
#
# ``debt_plans.monthly_cents`` is the TOTAL monthly outlay across the plan, not
# an extra on top of the minimums. That choice is load-bearing: an "extra"
# framing makes every figure depend on a minimum that itself moves with the
# balance, and the roll-in then has to be tracked separately, which is the
# classic double-count bug. One total, cascaded each month, needs no accumulator.
#
# NO SIMULATION RESULT IS STORED -- not the payoff month, not the interest
# total, not a per-debt cleared month. Every one of those is a function of
# balances that change with the next import, so a stored copy would be a stale
# number wearing the authority of the database. The projection is recomputed on
# demand and the tables hold only what the user typed.
_V110 = """
CREATE TABLE debt_terms (
    account_id         INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    apr                TEXT NOT NULL DEFAULT '0',    -- Decimal as TEXT, percent
    min_form           TEXT NOT NULL DEFAULT 'C',    -- A|B|C
    min_floor_cents    INTEGER NOT NULL DEFAULT 2500,
    min_pct            TEXT NOT NULL DEFAULT '1',    -- Decimal as TEXT, percent of balance
    credit_limit_cents INTEGER,                      -- NULL: unknown, utilization unavailable
    due_day            INTEGER,                      -- 1-31, NULL: unknown
    promo_rate         TEXT,                         -- Decimal as TEXT, NULL: no promotion
    promo_end_date     TEXT,                         -- ISO 'YYYY-MM-DD'
    promo_deferred     INTEGER NOT NULL DEFAULT 0,   -- deferred interest, not waived
    scheduled_id       INTEGER REFERENCES scheduled_payments(id) ON DELETE SET NULL,
    estimated          INTEGER NOT NULL DEFAULT 0,   -- terms synthesized, not entered
    updated_at         TEXT NOT NULL                 -- ISO date
);
CREATE TABLE debt_plans (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    strategy      TEXT NOT NULL DEFAULT 'avalanche',  -- snowball|avalanche|custom
    monthly_cents INTEGER NOT NULL DEFAULT 0,         -- TOTAL outlay, not an extra
    start_period  TEXT,                               -- ISO 'YYYY-MM'
    budget_id     INTEGER REFERENCES budgets(id) ON DELETE SET NULL,
    note          TEXT,
    created_at    TEXT NOT NULL                       -- ISO date
);
CREATE TABLE debt_plan_members (
    plan_id    INTEGER NOT NULL REFERENCES debt_plans(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    included   INTEGER NOT NULL DEFAULT 1,
    sort_index INTEGER,                    -- 'custom' only, NULL under the named strategies
    PRIMARY KEY (plan_id, account_id)
);
"""

# _V111: budget GROUPS -- a named pot inside one budget (SRD 5.12, the 2026-09
# revision in docs/budget_planner_design.md Part 3). The first release pointed
# ``budget_category_settings.group_id`` at a PARENT CATEGORY, reusing the tree
# for the roll-up; a tree whose Groceries and Dining are both top level has no
# parent to point at, and the only ways to name the pot were to label it after
# one member or to create an empty category that every picker would then show.
# So a group is its own row: a name, a bucket, a rollover mode, and its own
# monthly lines in ``budget_group_lines``. The pot holds the budget and the
# members hold the actuals (``budgets.budget_vs_actual``).
#
# ``budget_category_settings`` is REBUILT rather than altered because SQLite
# cannot retarget a foreign key in place, and ``group_id`` now references
# ``budget_groups`` instead of ``categories``. Every row is copied with
# ``group_id`` NULL: no shipped surface ever wrote a category-pointing group,
# only tests did, so nothing a user set is lost. ``budget_group_lines`` carries
# ``budget_id`` beside ``group_id`` (redundant with the group's own budget) so
# the start-month remap that wraps every per-period table can treat it exactly
# like ``budget_lines`` and ``budget_saving_lines``.
_V111 = """
CREATE TABLE budget_groups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id     INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    bucket        TEXT NOT NULL DEFAULT 'flex',    -- fixed|flex|nonmonthly
    rollover_mode TEXT NOT NULL DEFAULT 'none',    -- none|positive|both
    annual_cents  INTEGER NOT NULL DEFAULT 0,      -- nonmonthly: the yearly total
    UNIQUE (budget_id, name)
);
CREATE TABLE budget_group_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id    INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    group_id     INTEGER NOT NULL REFERENCES budget_groups(id) ON DELETE CASCADE,
    period       TEXT NOT NULL,                    -- ISO 'YYYY-MM'
    amount_cents INTEGER NOT NULL,
    UNIQUE (budget_id, group_id, period)
);
CREATE TABLE budget_category_settings_v111 (
    budget_id     INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    bucket        TEXT NOT NULL DEFAULT 'flex',   -- fixed|flex|nonmonthly
    rollover_mode TEXT NOT NULL DEFAULT 'none',   -- none|positive|both
    annual_cents  INTEGER NOT NULL DEFAULT 0,     -- nonmonthly: the yearly total
    group_id      INTEGER REFERENCES budget_groups(id) ON DELETE SET NULL,
    PRIMARY KEY (budget_id, category_id)
);
INSERT INTO budget_category_settings_v111
    (budget_id, category_id, bucket, rollover_mode, annual_cents, group_id)
    SELECT budget_id, category_id, bucket, rollover_mode, annual_cents, NULL
      FROM budget_category_settings;
DROP TABLE budget_category_settings;
ALTER TABLE budget_category_settings_v111 RENAME TO budget_category_settings;
"""

# _V112: the one-page Budget (SRD 5.12, docs/budget_one_page_design.md). Two
# small tables. ``budget_line_order`` is the user's own priority order of the
# lines on the page -- income, giving, saving, fixed, variable in the lesson's
# default, then whatever the user drags it to -- keyed by what the line is
# (``category`` | ``group`` | ``account``) and its id; a line with no row sorts
# by the default. ``budget_other_lines`` is the planned amount for the page's
# last line, "Everything else": spending the plan has no line for, which the
# lesson calls miscellaneous. It is per month like every other planned figure
# and keyed by budget and period alone, because it stands for no category.
# Income lines need no table: a ``budget_lines`` row on an income category,
# marked ``income`` in ``budget_category_settings.bucket``.
_V112 = """
CREATE TABLE budget_line_order (
    budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,                       -- category|group|account
    ident     INTEGER NOT NULL,
    position  INTEGER NOT NULL,
    PRIMARY KEY (budget_id, kind, ident)
);
CREATE TABLE budget_other_lines (
    budget_id    INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    period       TEXT NOT NULL,                    -- ISO 'YYYY-MM'
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (budget_id, period)
);
"""

# _V113: a PAYEE line on the Budget page (SRD 5.12). A mortgage payment is a
# split -- principal to the loan, interest, escrow -- and a budgeter who wants
# to treat the payment as one line names the payee instead of the categories.
# ``budget_groups.payee_match`` is the text the payee must contain; a group
# with it is a payee line with no members, whose actual is every matching
# payment out of a spending account counted WHOLE, and whose payments' category
# legs are taken out of the category actuals so the money is counted once
# (``budgets.payee_payments``). A column on the group rather than a table of its
# own, because a payee line IS a pot with a name, lines, a kind and an order --
# everything a group already has -- differing only in how its actual is found.
_V113 = """
ALTER TABLE budget_groups ADD COLUMN payee_match TEXT;
"""

# _V114: a budget's SCOPE (SRD 5.12). A household ledger may carry a business
# checking account and business categories; their spending is real but is not
# the household budget's, and without a scope it would land in "Everything
# else". ``budget_accounts`` lists the spending accounts a budget covers (no
# rows = every spending account, the default a one-account household never
# touches); ``budget_excluded_categories`` lists categories the budget leaves
# out entirely - not offered, not counted, not proposed. Both per budget, like
# every other budget decision, so a business plan and a household plan can
# divide the same ledger between them.
_V114 = """
CREATE TABLE budget_accounts (
    budget_id  INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (budget_id, account_id)
);
CREATE TABLE budget_excluded_categories (
    budget_id   INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (budget_id, category_id)
);
"""

# _V115: one transfer keyword, one rule PER PAYING ACCOUNT (SRD 5.5g). Two cards
# from one issuer can send byte-identical bank text ("ANYBANK AUTOPAY PAYMENT")
# and be paid from different accounts; the only thing that tells them apart is
# which account the payment left. Migration 14 made ``keyword`` unique, so a
# rule scoped to one account (the ``account_id`` condition of migration 40)
# could not sit beside the same keyword scoped to another. Uniqueness is now
# (keyword, scope), kept by ``transfer_rules.upsert_rule``, the only writer; the
# index is deliberately NOT unique because ``account_id`` is ON DELETE SET NULL,
# and deleting a scope account must un-scope its rules, not fail on a collision
# with an existing global rule (the learner's history check then withholds a
# keyword that two rules disagree on).
_V115 = """
DROP INDEX idx_transfer_rules_keyword;
CREATE INDEX idx_transfer_rules_keyword ON transfer_rules(keyword, account_id);
"""

# _V116: every learned behavior is a decision tree; the keyword tables go (SRD
# 5.5b, the user's ruling). The per-payee category tree (v55/v56) becomes the one
# learner for what goes in the Category cell, and that cell holds either a
# category or a transfer account, so a vote is now for one or the other:
# ``category_id`` and ``account_id`` are both nullable foreign keys, exactly one
# set, each ON DELETE CASCADE so deleting a category or an account drops its
# votes the way deleting a category always did. Existing votes are carried
# across unchanged -- the trees were grown only from what the user accepted in
# review, and app startup never rebuilds them, so dropping them would lose that.
#
# ``category_rules`` and ``transfer_rules`` are dropped: a global keyword rule
# learned from one accept filed one card's payment to another card that shares
# its bank text, and a hand-written keyword rule is the Quicken mechanism the
# user asked to be rid of. The learned and user-pinned payee -> category rows
# of ``import_mappings`` go with them (the tree's per-payee tally is the same
# evidence with counts); its ``account_link`` rows, the importer's own memory of
# which source account is which, are not learning and stay.
#
# ``category_nodes.common`` holds the tokens every row that stopped at a node
# shared. A split may only descend on a token the new row has and those rows do
# not all share: splitting on a token everyone carries (the trie once split on
# the first-ranked token, whatever it was) sent identical bank text for two
# different cards down one branch, and the tree then confidently named the
# second card for both. NULL on a node grown before v116 (the old split rule).
#
# ``category_forgotten`` remembers what the user told Mammon to forget -- a
# payee, or one category or account for a payee -- up to the newest transaction
# at that moment, so an explicit seed-from-history cannot quietly resurrect it.
# ``category_meta`` holds one-time flags; ``seed_v116`` asks for the transfer
# votes and per-account trees to be grown once from the accepted review rows,
# which only Python can do (``category_tree.ensure_seeded``), and only for a
# ledger that has accepted rows to grow them from.
_V116 = """
ALTER TABLE category_nodes ADD COLUMN common TEXT;
DROP TABLE category_rules;
DROP TABLE transfer_rules;
DELETE FROM import_mappings WHERE COALESCE(source, '') IN ('user', 'learned');

CREATE TABLE category_node_labels_v116 (
    id          INTEGER PRIMARY KEY,
    node_id     INTEGER NOT NULL REFERENCES category_nodes(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1,
    CHECK ((category_id IS NULL) <> (account_id IS NULL))
);
INSERT INTO category_node_labels_v116(id, node_id, category_id, count)
    SELECT id, node_id, category_id, count FROM category_node_labels;
DROP TABLE category_node_labels;
ALTER TABLE category_node_labels_v116 RENAME TO category_node_labels;
CREATE UNIQUE INDEX idx_category_node_label ON category_node_labels(
    node_id, IFNULL(category_id, 0), IFNULL(account_id, 0));

CREATE TABLE category_payee_stats_v116 (
    id          INTEGER PRIMARY KEY,
    payee_key   TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1,
    CHECK ((category_id IS NULL) <> (account_id IS NULL))
);
INSERT INTO category_payee_stats_v116(id, payee_key, category_id, count)
    SELECT id, payee_key, category_id, count FROM category_payee_stats;
DROP TABLE category_payee_stats;
ALTER TABLE category_payee_stats_v116 RENAME TO category_payee_stats;
CREATE UNIQUE INDEX idx_category_payee_stat ON category_payee_stats(
    payee_key, IFNULL(category_id, 0), IFNULL(account_id, 0));

CREATE TABLE category_token_labels_v116 (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1,
    CHECK ((category_id IS NULL) <> (account_id IS NULL))
);
INSERT INTO category_token_labels_v116(id, token, category_id, count)
    SELECT id, token, category_id, count FROM category_token_labels;
DROP TABLE category_token_labels;
ALTER TABLE category_token_labels_v116 RENAME TO category_token_labels;
CREATE UNIQUE INDEX idx_category_token_label ON category_token_labels(
    token, IFNULL(category_id, 0), IFNULL(account_id, 0));

CREATE TABLE category_forgotten (
    payee_key      TEXT NOT NULL,
    label          INTEGER NOT NULL,      -- 0 = the whole payee
    through_txn_id INTEGER NOT NULL,
    PRIMARY KEY (payee_key, label)
);
CREATE TABLE category_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT INTO category_meta(key, value)
    SELECT 'seed_v116', 'pending'
    WHERE EXISTS (SELECT 1 FROM review_items WHERE state = 'accepted');
"""


MIGRATIONS: list[str] = [
    _V1,
    _V2,
    _V3,
    _V4,
    _V5,
    _V6,
    _V7,
    _V8,
    _V9,
    _V10,
    _V11,
    _V12,
    _V13,
    _V14,
    _V15,
    _V16,
    _V17,
    _V18,
    _V19,
    _V20,
    _V21,
    _V22,
    _V23,
    _V24,
    _V25,
    _V26,
    _V27,
    _V28,
    _V29,
    _V30,
    _V31,
    _V32,
    _V33,
    _V34,
    _V35,
    _V36,
    _V37,
    _V38,
    _V39,
    _V40,
    _V41,
    _V42,
    _V43,
    _V44,
    _V45,
    _V46,
    _V47,
    _V48,
    _V49,
    _V50,
    _V51,
    _V52,
    _V53,
    _V54,
    _V55,
    _V56,
    _V57,
    _V58,
    _V59,
    _V60,
    _V61,
    _V62,
    _V63,
    _V64,
    _V65,
    _V66,
    _V67,
    _V68,
    _V69,
    _V70,
    _V71,
    _V72,
    _V73,
    _V74,
    _V75,
    _V76,
    _V77,
    _V78,
    _V79,
    _V80,
    _V81,
    _V82,
    _V83,
    _V84,
    _V85,
    _V86,
    _V87,
    _V88,
    _V89,
    _V90,
    _V91,
    _V92,
    _V93,
    _V94,
    _V95,
    _V96,
    _V97,
    _V98,
    _V99,
    _V100,
    _V101,
    _V102,
    _V103,
    _V104,
    _V105,
    _V106,
    _V107,
    _V108,
    _V109,
    _V110,
    _V111,
    _V112,
    _V113,
    _V114,
    _V115,
    _V116,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: str | Path, key: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection with the conventions Mammon relies on everywhere.

    Opened through :mod:`mammon.sqldriver`, which is SQLCipher when it is installed and
    the standard library otherwise. No key is set here: an unkeyed SQLCipher database
    is an ordinary plain SQLite file, so this is the same file it has always been.
    Encryption is applied to a database by converting it (see :mod:`mammon.encryption`),
    never by opening it differently.

    ``Row`` must come from the SAME driver as the connection -- ``sqlite3.Row`` on a
    SQLCipher cursor raises TypeError."""
    conn = sqldriver.connect(str(path))
    if key:
        # Before every other statement: the key configures the codec that reads
        # page 1. Raises DatabaseError when the key is wrong (sqldriver.apply_key).
        sqldriver.apply_key(conn, key)
    conn.row_factory = sqldriver.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WRITE-AHEAD LOGGING, and a relaxed fsync to go with it. Measured, not
    # cosmetic: this codebase commits once per WRITTEN ROW (ledger.add_transaction
    # commits, then its checkpoint cascade commits again), so importing one 1.1 MB
    # yearly Quicken QIF issued ~2,700 commits and spent 79% of its wall clock
    # INSIDE commit() on an encrypted (SQLCipher) file -- 46s for a single file,
    # putting a 32-file full-history migration into the tens of minutes. In WAL a
    # commit appends to a log instead of rewriting and fsyncing a rollback
    # journal: the same import went 5.68s -> 0.40s on a fresh ledger, and
    # 1.63s -> 0.31s on one that already had history.
    #
    # This is safe with everything that copies a Mammon database, which was
    # verified before turning it on rather than assumed:
    #   * `backup.create_backup` uses SQLite's ONLINE BACKUP API (conn.backup),
    #     which reads through the WAL and writes a fully checkpointed, standalone
    #     file; its page-delta diffing then reads that STAGED copy, never the live
    #     database. A backup that copied only the .db file WOULD have captured a
    #     stale ledger -- that is the trap WAL sets, and this code avoids it.
    #   * `encryption` converts via ATTACH + sqlcipher_export from an OPEN
    #     connection, so it likewise sees committed data through the WAL.
    # WAL keeps `<db>-wal` and `<db>-shm` beside the database between commits.
    # Both are transient, and a clean close checkpoints and removes them.
    #
    # Durability is deliberately SPLIT from the journal mode, because the two are
    # independent and only one of them costs anything. WAL above is free: it
    # cannot lose a commit and cannot corrupt the file. `synchronous` is the part
    # that trades safety for speed, so it is set STRICT here and relaxed only for
    # the duration of a bulk import (see :func:`bulk_write`).
    #
    # FULL means every commit is forced to the physical disk before it returns:
    # nothing the user types can be lost, which is the right default for a ledger
    # keyed in by hand. NORMAL (what a bulk import switches to) lets the OS flush
    # in its own time, so a power loss can lose the most recent commits --
    # acceptable for an import, whose source file is still sitting on disk and can
    # simply be re-run, and never acceptable for hand entry.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except Exception:
        # Switching journal mode WRITES to the database, so it cannot be done to
        # a read-only ledger. Opening one must still work; it simply keeps the
        # rollback journal. (For ":memory:" the pragma is a no-op that reports
        # "memory" rather than raising.) This is the one deliberately tolerant
        # line in this function: the alternative is refusing to open a file the
        # user can legitimately only read.
        pass
    # Per CONNECTION, not a property of the file -- so it must be set on every
    # open, unlike journal_mode which persists in the database header.
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextlib.contextmanager
def bulk_write(conn):
    """Relax fsync for a BULK, RE-RUNNABLE write (an import), then restore it.

    Why this is scoped rather than global: `synchronous=FULL` is what guarantees
    that a transaction the user typed survives a power cut, and that guarantee is
    worth keeping for every ordinary write. An import is the one case where it
    buys nothing -- the rows come from a file that is still on disk, so the
    remedy for a crash mid-import is to run it again, not to recover it -- and
    where the cost is large, because this codebase commits once per written row
    (importing one yearly Quicken QIF issued ~2,700 commits; forcing each to
    disk on an encrypted file took 46s for a single file).

    Restores the PRIOR value rather than assuming FULL, so nesting is safe and an
    unkeyed/`:memory:`/read-only connection keeps whatever it actually had.
    NORMAL is safe against corruption under WAL -- only the newest commits are at
    risk -- which is exactly the trade described in :func:`connect`."""
    prior = conn.execute("PRAGMA synchronous").fetchone()[0]
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
    finally:
        # Numeric form: PRAGMA synchronous reads back as an int (0=OFF, 1=NORMAL,
        # 2=FULL, 3=EXTRA) and accepts the same, so the exact prior level is
        # restored without mapping it back through a name.
        conn.execute(f"PRAGMA synchronous = {int(prior)}")


def init_db(path: str | Path, key: Optional[str] = None) -> sqlite3.Connection:
    """Create or upgrade the Mammon database at ``path`` and return a connection.

    Idempotent: applies only migrations newer than the file's user_version.
    """
    conn = connect(path, key)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for target in range(current, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[target])
        # user_version does not accept a bound parameter.
        conn.execute(f"PRAGMA user_version = {target + 1}")
    conn.commit()
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
    c = init_db(target)
    print(f"Mammon DB initialized at {target!r} (schema v{SCHEMA_VERSION})")
    for name in sorted(table_names(c)):
        print("  ", name)
