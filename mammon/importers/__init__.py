"""mammon.importers -- pluggable importers that all converge on the ledger.

Public API:
    import_file(conn, path, provider=None, source_format=None, account=None)
        Parse a file (format inferred from extension unless given) and ingest it.
    import_records(conn, records, ...)
        Ingest already-parsed NormalizedTxn objects (used by tests and by any
        in-memory source such as a live webSlinger run).

Each parser is a pure function ``parse(text, default_account=None) ->
list[NormalizedTxn]``; the shared insert/dedup/transfer logic lives in
mammon.importers.core.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from mammon.importers.core import import_records
from mammon.importers.csvimp import parse_csv
from mammon.importers.jsonimp import parse_json
from mammon.importers.ofx import (
    parse_ofx,
    parse_ofx_positions,
    unmapped_investment_actions,
)
from mammon.importers.qif import QifExtras, parse_qif
from mammon.importers.record import ImportResult, NormalizedTxn

__all__ = [
    "import_file",
    "import_records",
    "import_single_account",
    "distinct_account_names",
    "multi_account_file",
    "NormalizedTxn",
    "ImportResult",
    "PARSERS",
]


def distinct_account_names(records, default_account: Optional[str] = None) -> set:
    """Distinct owning-account names across ``records`` (a transfer's counterparty
    is NOT counted -- only ``external_account``, the account a row belongs to).

    ``len(...) <= 1`` means a SINGLE-account import: every transfer is one-sided
    (the counterparty's own leg is absent), so it takes the review-list path where
    the mirror is created and matches prevent double-entry. ``> 1`` means a
    multi-account QIF that carries BOTH legs, which :func:`import_records` links
    directly without fabricating a mirror."""
    names = {(r.external_account or "").strip() for r in records}
    names.discard("")
    if not names and default_account:
        names.add(default_account.strip())
    return names

def multi_account_file(
    path: str | Path,
    source_format: Optional[str] = None,
    account: Optional[str] = None,
) -> bool:
    """Decide whether an import FILE must bypass review and import DIRECTLY.

    This is the ONE routing rule shared by every file-sourced import (toolbar
    Import…, and a webSlinger EXPORT run that dropped a file): the ONLY direct
    path is a MULTI-ACCOUNT file -- a QIF carrying BOTH legs of its transfers
    across more than one owning account (they collapse into linked pairs), or a
    QIF bearing a securities / price master that only the bulk importer can apply.
    Every single-account file -- cash OR investment, in ANY format -- returns
    ``False`` so the caller routes it through the import review queue instead
    (user: "no real difference between direct-download and file data; both must
    land in review").

    Parsing failures return ``False`` (route to review, where the real importer
    surfaces any error) rather than raising, so a malformed file never silently
    slips into a direct write."""
    try:
        p = Path(path)
        fmt = source_format or _EXT_FORMAT.get(p.suffix.lower())
        if fmt is None or fmt not in PARSERS:
            return False
        text = _decode(p.read_bytes())
        if fmt == "qif":
            extras = QifExtras()
            records = parse_qif(text, default_account=account, collector=extras)
            if extras.securities or extras.prices:
                return True
            return len(distinct_account_names(records, account)) > 1
        records = PARSERS[fmt](text, default_account=account)
        return len(distinct_account_names(records, account)) > 1
    except Exception:
        return False


PARSERS = {
    "qif": parse_qif,
    "ofx": parse_ofx,
    "json": parse_json,
    "csv": parse_csv,
}

# Delimited sources all route to the "csv" parser, which sniffs the actual
# delimiter (comma/semicolon/tab/pipe) rather than trusting the extension -- banks
# ship the same grid as .csv, .tsv or a bare .txt, and the column-map wizard has
# to be reachable for all of them.
_EXT_FORMAT = {
    ".qif": "qif",
    ".ofx": "ofx",
    ".qfx": "ofx",
    ".json": "json",
    ".csv": "csv",
    ".tsv": "csv",
    ".tab": "csv",
    ".txt": "csv",
}


def import_file(
    conn,
    path: str | Path,
    provider: Optional[str] = None,
    source_format: Optional[str] = None,
    account: Optional[str] = None,
    account_type: Optional[str] = None,
) -> ImportResult:
    """Import a file on disk. ``account`` names the account for single-account
    formats (OFX/CSV/JSON) and is the fallback for a QIF without ``!Account``
    blocks; multi-account QIF files carry their own account names."""
    p = Path(path)
    fmt = (source_format or _EXT_FORMAT.get(p.suffix.lower()))
    if fmt is None:
        raise ValueError(f"cannot infer import format from {p.suffix!r}; pass source_format")
    if fmt not in PARSERS:
        raise ValueError(f"unknown import format {fmt!r}")

    raw = p.read_bytes()
    text = _decode(raw)
    # QIF can carry a security master + price history; capture them so investment
    # accounts get priced holdings. Other formats have no such sidecar.
    extras = QifExtras() if fmt == "qif" else None
    positions = None
    if fmt == "qif":
        records = parse_qif(text, default_account=account, collector=extras)
    else:
        records = PARSERS[fmt](text, default_account=account)
    if fmt == "ofx":
        # OFX statements carry an <INVPOSLIST> holdings snapshot alongside the
        # transactions: capture its unit prices and reconcile reported shares.
        positions = [
            (pos.symbol, pos.date, pos.units, pos.unitprice)
            for pos in parse_ofx_positions(text)
        ]
    result = import_records(
        conn,
        records,
        provider=provider or account or p.stem,
        source_format=fmt,
        filename=p.name,
        file_hash=hashlib.sha256(raw).hexdigest(),
        default_account=account,
        default_account_type=account_type or "checking",
        securities=extras.securities if extras else None,
        prices=extras.prices if extras else None,
        positions=positions,
        categories=extras.categories if extras else None,
    )
    # Post-import AUDIT: surface any OFX investment action types the parser could
    # not map (they were dropped, not silently defaulted into cash-in).
    if fmt == "ofx":
        result.unmapped_actions = unmapped_investment_actions(text)
    return result


def import_single_account(
    conn,
    *,
    account: str,
    account_type: str = "checking",
    path: str | Path | None = None,
    records: Optional[list] = None,
    provider: Optional[str] = None,
    source_format: Optional[str] = None,
    finalize: bool = True,
):
    """Import ONE account's file/records THROUGH the review list -- a single-account
    QIF is treated exactly like a live download.

    Give either ``path`` (parsed with the format inferred from its suffix, or
    ``source_format``) or already-parsed ``records``, plus the ``account`` name the
    rows belong to (get-or-created). Every transfer leg in a single-account import
    is one-sided in the file, so the review path creates the counterparty mirror on
    accept while matching an already-present counterparty leg instead of
    double-entering it (see :func:`mammon.import_review.import_records_via_review`).

    With ``finalize`` True (default) the review is accepted end-to-end and a
    ``{"added", "matched"}`` summary is returned; with ``finalize`` False the
    classified :class:`~mammon.import_review.ReviewEntry` list is returned for a UI
    to review first. Multi-account QIF (both legs present) should NOT come here --
    use :func:`import_records`, which links the two legs without a mirror."""
    from mammon import import_review, ledger

    if records is None:
        if path is None:
            raise ValueError("import_single_account needs either path or records")
        p = Path(path)
        fmt = (source_format or _EXT_FORMAT.get(p.suffix.lower()))
        if fmt is None:
            raise ValueError(
                f"cannot infer import format from {p.suffix!r}; pass source_format")
        if fmt not in PARSERS:
            raise ValueError(f"unknown import format {fmt!r}")
        text = _decode(p.read_bytes())
        if fmt == "qif":
            records = parse_qif(text, default_account=account)
        elif fmt == "csv":
            # Tabular sources honour a SAVED column-map profile (fingerprinted by
            # header signature) so a known/confirmed format imports with no prompt;
            # an unknown format is auto-interpreted here and the UI may preview it.
            from mammon.importers import tabular
            plan = tabular.plan_tabular(
                conn, text, default_account=account, account_type=account_type)
            records = plan.records if plan is not None else parse_csv(
                text, default_account=account)
        else:
            records = PARSERS[fmt](text, default_account=account)

    # Get-or-create the owning account (single-account import: all rows are its own).
    account_id = None
    for acct in ledger.list_accounts(conn, include_closed=True):
        if str(acct["name"]).strip().lower() == account.strip().lower():
            account_id = int(acct["id"])
            break
    if account_id is None:
        account_id = ledger.create_account(conn, account.strip(), account_type)

    if finalize:
        return import_review.import_records_via_review(conn, account_id, records)
    return import_review.build_review_from_records(conn, account_id, records)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")
