"""Bank-feed boilerplate shared by Mammon's learned trees.

Raw bank ``statementDescription`` text is gobbledygook ("POS DEBIT 1234
AMAZON.COM SEATTLE WA"). Both learned trees -- payee renaming
(:mod:`mammon.rename_tree`) and the Category cell (:mod:`mammon.category_tree`)
-- rank the tokens of that text by how well they discriminate, and both push the
words below to the END of that ranking: they appear in every kind of row, so a
tree that split on them would be splitting on nothing.

This module once held the keyword-rule machinery too -- extraction,
refinement, whole-token matching and per-rule conditions -- for the
``category_rules`` and ``transfer_rules`` tables. Every learned behavior is now a
tree (the user's ruling, 2026-10) and migration 116 dropped both tables, so only
the shared word list is left.
"""
from __future__ import annotations

__all__: list[str] = []

# Non-merchant filler that shows up in bank feeds. Kept UPPER-CASED.
# Deliberately conservative -- better to keep an odd token than to bury a real
# merchant name.
_NOISE = {
    "POS", "ACH", "ATM", "DEBIT", "CREDIT", "PURCHASE", "PAYMENT", "PMT",
    "WITHDRAWAL", "DEPOSIT", "AUTOMATIC", "CARD", "CHECKCARD", "CHECK", "VISA",
    "MASTERCARD",
    "TRANSACTION", "TRANS", "REF", "AUTH", "RECURRING", "ONLINE", "MOBILE",
    "BILLPAY", "BILL", "PAY", "WEB", "PPD", "CCD", "EFT", "DDA", "PIN", "SEC",
    "IAT", "TEL", "DES", "INDN", "THE", "AND", "FOR", "USA", "COM", "WWW", "LLC",
    "INC", "XXXX", "XXXXX", "XXXXXX", "NUM", "ID", "NO",
}
