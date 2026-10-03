"""Mammon -- an open personal-finance ledger with a classic register.

Public entry points live in submodules:
- ``mammon.db``      : schema definition + init/migration.
- (coming) ``mammon.ledger``   : accounts, transactions, transfers, balances.
- (coming) ``mammon.importers`` : qif / ofx / json / csv -> normalized records.
"""

from mammon.db import SCHEMA_VERSION, connect, init_db, table_names

__all__ = ["SCHEMA_VERSION", "connect", "init_db", "table_names"]
