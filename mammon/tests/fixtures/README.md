# Anonymized institution import fixtures

Small, fully **anonymized** sample downloads in the export shapes common US
banks, card issuers and brokerages produce. Account numbers, merchant names and
amounts are fake; every file is trimmed to a handful of rows.

They exist so `tests/test_downloads.py` can prove two things against the real
importer + cutover-guard code (no browser, no live account):

1. each fixture imports cleanly (`import_download_file`, added > 0, no errors);
2. a first live pull does **not** duplicate history across the one-time
   Quicken-migration seam -- rows dated on/before the per-account
   `accounts.cutover_date` watermark are dropped even though the migrated
   history is fitid-less.

Shared date scheme (so one cutover spec covers every cash fixture):
transactions on 2026-07-05, 2026-07-08 (both <= cutover 2026-07-10) and
2026-07-15 (> cutover). Amounts: -50.00 / -12.34 / -7.89.

| file | format | account_type |
|------|--------|--------------|
| anytown_cu_checking.ofx | OFX 1.x SGML | checking |
| anytown_cu_savings.ofx  | OFX 1.x SGML | savings |
| chase.qfx                  | QFX (OFX SGML) | credit |
| citibank.qfx               | QFX (OFX SGML) | credit |
| bank_of_america.qfx        | QFX (OFX SGML) | checking |
| wells_fargo.csv            | CSV (no FITID) | credit |
| discover.csv               | CSV (no FITID) | credit |
| fidelity.csv               | CSV (investment) | investment |
