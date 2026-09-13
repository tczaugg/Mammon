# Quicken import research: direct .QDF read vs. QIF for decades of data

Prepared 2026-08-09 in response to the user's question: *"My preference is that Mammon
read the Quicken data files directly. I don't think QIF will be effective for
decades of data - research which apps import Quicken files directly and whether
QIF has worked for people with decades of history."*

Bottom line up front:
- **No independent application reads a Quicken `.QDF` file directly.** The only
  tool that converts a raw `.QDF` is Intuit's own **QuickBooks Desktop** (a
  sibling product, business accounting - not useful as a personal-finance
  target). Every independent personal-finance app - Moneydance, GnuCash,
  Banktivity, SEE Finance - explicitly cannot open a `.QDF` and requires you to
  export **QIF** (or, on Quicken Mac, **QMTF**) from inside Quicken first.
- **QIF for complex, multi-decade data is real but lossy and labor-intensive.**
  The best-documented 25-year migration was ultimately abandoned as "not
  feasible" without running old and new side by side, and required custom code
  plus manual fixing.
- So neither framing is clean. My recommendation (end of doc) is a cheap,
  time-boxed **direct-read spike on your actual 2017 file** - which honors your
  preference and could be a big win - with a **per-account QIF/CSV + reconcile**
  path as the proven fallback.

## 1. Can anything read the `.QDF` directly?

The `.QDF` is a **proprietary, encrypted** container. Technically it is a **zip
of a multi-file data set** whose records are stored with a **SQLite** engine -
so under the wrapper it is a database, but the file is encrypted and the layout
is undocumented and versioned. Reverse-engineering is actively discouraged
("don't take it apart and hope to put it back together, it won't turn out
well"), the encryption is opaque (the only public study, from 2002, found it
weak at that time), and there is **no third-party library** - the Python
`quiffen` library, for instance, reads QIF only, not `.QDF`.

Who reads it:
- **QuickBooks Desktop** - the one app with a direct `File > Utilities >
  Convert > From Quicken` path (same corporate lineage historically). Converts
  to a *business* ledger, not a personal-finance register.
- **Moneydance** - "cannot import Quicken Data Files (QDF) or Quicken backups...
  it is necessary to use Quicken itself to export the data... this is generally
  the QIF format."
- **GnuCash** - "You cannot import a QDF file into GnuCash, and there are no
  plans to add QDF support."
- **Banktivity / SEE Finance** - accept **QMTF** (a Quicken *Mac* export), not
  the Windows `.QDF`.

Implication for Mammon: building a direct `.QDF` reader means defeating the file
encryption and reverse-engineering an undocumented, per-version, zipped SQLite
layout - work that mature independent products all declined to attempt. High
risk, brittle across Quicken versions, and it may never open an encrypted file.

## 2. Has QIF worked for people with decades of data?

Mixed, trending poor as data gets complex. The most detailed first-hand account
(Kevin Kleinfelter migrating ~25 years of Quicken history to Moneydance via QIF)
hit, and had to hand-fix:
- **Retirement transfers** (ContribX/WithdrwX) imported as duplicate paired
  in-then-out transactions.
- **Self-transfers** created phantom accounts with negative balances.
- **Investment actions**: DivX dividends produced spurious reversing entries;
  interest income imported with **inverted signs**.
- **Quicken "placeholder" transactions** (auto-created when a downloaded balance
  disagreed with the register) corrupted the target data - "an entire long day
  fixing 4 placeholders."
- **Broken dates** (`0/00/0000`) and mismatched transfer endpoints.
He wrote **code to transform the QIF records** and still concluded the project
was "simply not feasible" without parallel operation, and abandoned it.

Corroborating reports: transactions imported with **wrong/future dates**; a
15-year account where **everything after Dec 2013 silently failed to import**;
and the structural killer - **QIF records carry no IDs, so there is no reliable
way to prevent duplicates** (OFX/QFX was created specifically to fix QIF's
problems).

And a Quicken-Windows-specific catch: modern Quicken (2006+, so including
**2017**) **restricts QIF for banking and brokerage accounts** - QIF
import/export is officially supported only for cash, asset and liability
accounts, *not* checking/savings/credit or 401(k)/brokerage, pushing users to
OFX/QFX and Quicken's own **QXF** (which is Quicken-to-Quicken only, so it does
not help a third party). Getting bank/investment history out as QIF from 2017
often needs per-account workarounds (temporarily retyping accounts, etc.).

QIF is fine for **simple cash-account history**; it degrades on investments,
transfers, and Quicken's placeholder/download artifacts - exactly what 40 years
of real use accumulates.

## 3. Recommendation for Mammon

1. **Do not build a general `.QDF` reader.** It is not a good investment
   (encrypted, undocumented, versioned, no library, industry-wide declined).
2. **Honor the "read it directly" preference with a cheap, time-boxed spike on
   your ACTUAL file.** Because the container is SQLite underneath, the real
   question is only whether *your* 2017 `.QDF` (or its unzipped contents, with
   any file password you set removed) can be opened as a SQLite database. If it
   opens, direct high-fidelity reading becomes viable and we build to your real
   schema. If it is encrypted/obfuscated shut, we stop and fall back. This is a
   few hours, not a project, and it settles the question empirically for you.
3. **Proven fallback = per-account QIF/CSV export + reconcile.** Mammon's importer
   already does fitid/fuzzy dedup, Quicken mirror-transfer collapse, and
   per-account balance reconciliation. Add a small cleanup pass for the known
   QIF artifacts above (transfer de-duplication, investment sign flips,
   placeholder handling, `0/00/0000` dates), and drive it by **reconciling each
   account's imported balance to Quicken's own balance**, fixing discrepancies
   until they tie. CSV export is the reliable path for cash accounts that
   Quicken won't QIF-export.
4. **The one decisive input is your real data.** A copy of the `.QDF` (for the
   SQLite spike) plus a QIF and/or CSV export of ~3 representative accounts (one
   bank, one credit card, one investment) lets me measure true fidelity on *your*
   history instead of relying on strangers' reports - and tells us in one pass
   whether direct-read is on the table or QIF-plus-cleanup is the road.

## Sources
- [QDF File - Quicken Data File (fileformat.com)](https://docs.fileformat.com/data/qdf/)
- [Can Moneydance import Quicken QDF files? (Infinite Kind)](https://infinitekind.tenderapp.com/discussions/switching-from-another-personal-finance-program/5453-can-moneydance-import-quicken-qdf-files)
- [Notes on Importing 25 Years of Complex Quicken Data (Infinite Kind)](https://infinitekind.tenderapp.com/discussions/problems/57338-notes-on-importing-25-years-of-complex-quicken-data)
- [Convert Quicken data to QuickBooks Desktop (Intuit)](https://quickbooks.intuit.com/learn-support/en-us/help-article/import-export-data-files/convert-quicken-data-quickbooks-desktop/L3U83Vw9O_US_en_US)
- [Quicken Migration (GnuCash wiki)](https://wiki.gnucash.org/wiki/Quicken_Migration)
- [Current state of QDF local file encryption (Quicken community)](https://getsatisfaction.com/quickencommunity/topics/current-state-of-qdf-local-file-encryption)
- [Quiffen - Python QIF (not QDF) library](https://quiffen.readthedocs.io/en/latest/index.html)
- [Importing data from a QIF file - Quicken Windows help](https://info.quicken.com/win/why-can-t-i-import-data-from-a-qif-file-into-a-ban)
- [FAQ: How to Import QIF Files Into Non-cash Accounts, post-Q2004 (Quicken community)](https://community.quicken.com/discussion/7150750/faq-how-to-import-qif-files-into-non-cash-accounts-post-q2004)
- [Transactions imported from QIF with wrong dates (Infinite Kind)](https://infinitekind.tenderapp.com/discussions/problems/198-transactions-are-imported-from-qif-with-wrong-dates)
