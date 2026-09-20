"""TXF v042 export (``mammon.reports.custom_export``).

Everything here is synthetic: made-up institutions, made-up amounts, and refnums
from the 9001+ range that no real TXF form uses. The point of the file is the
BOUNDARY -- cents to decimal strings, ISO dates to MM/DD/YYYY, and the two
exclusion rules (no refnum, no record; COMPUTED, never a record) -- so the
assertions are on the bytes.
"""

import pytest

from mammon import db, ledger
from mammon.reports import custom, custom_export

START = "2025-01-01"
END = "2025-12-31"

WAGES_A = "TX:wages ANON Corp"
WAGES_B = "TX:wages ANON LLC"
INTEREST = "TX:interest"
SUBTOTAL = "TX:subtotal"
TOTAL = "TX:total"


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "export.db")
    yield c
    c.close()


@pytest.fixture
def tax_report(conn):
    """Two employers sharing one refnum on different copies, one interest line,
    a subtotal carrying no refnum, and a COMPUTED grand total that wrongly
    carries one."""
    checking = ledger.create_account(conn, "Everyday", "checking",
                                     opening_balance=0,
                                     opening_date="2024-12-01")
    cats = {
        WAGES_A: ledger.resolve_category(conn, "Wages:ANON Corp"),
        WAGES_B: ledger.resolve_category(conn, "Wages:ANON LLC"),
        INTEREST: ledger.resolve_category(conn, "Interest income"),
        SUBTOTAL: ledger.resolve_category(conn, "Other income"),
    }
    for date, amount, name in (("2025-03-03", 40000_00, WAGES_A),
                               ("2025-04-04", 1250_50, WAGES_B),
                               ("2025-05-05", 312_45, INTEREST),
                               ("2025-06-06", -99_99, SUBTOTAL)):
        ledger.add_transaction(conn, checking, date, amount,
                               category_id=cats[name])

    rid = custom.create_report(conn, "Tax 2025", kind="tax",
                               range_kind="fixed",
                               range_start=START, range_end=END)
    ids = {}
    specs = [
        (WAGES_A, {"txf_refnum": 9001, "txf_copy": 1, "txf_format": 1}),
        (WAGES_B, {"txf_refnum": 9001, "txf_copy": 2, "txf_format": 1}),
        (INTEREST, {"txf_refnum": 9002}),
        (SUBTOTAL, {}),                       # no refnum: never exported
    ]
    for name, extra in specs:
        ids[name] = custom.add_item(conn, rid, name, "SOSC", **extra)
        custom.set_item_categories(conn, ids[name], [cats[name]])
    # A refnum on a COMPUTED line is a mistake the export must survive: the
    # number is real, but TXF has no way to say "this line is those lines".
    ids[TOTAL] = custom.add_item(
        conn, rid, TOTAL, "COMPUTED", txf_refnum=9003,
        expr="{%s} + {%s} + {%s}" % (WAGES_A, WAGES_B, INTEREST))
    return {"id": rid, "ids": ids}


# --------------------------------------------------------------------------
# The conversions
# --------------------------------------------------------------------------

def test_format_amount_is_a_plain_signed_decimal():
    assert custom_export.format_amount(0) == "0.00"
    assert custom_export.format_amount(1) == "0.01"
    assert custom_export.format_amount(40000_00) == "40000.00"
    assert custom_export.format_amount(1250_50) == "1250.50"
    assert custom_export.format_amount(-99_99) == "-99.99"
    assert custom_export.format_amount(123456789_01) == "123456789.01"
    for cents in (0, 1, -1, 40000_00, -99_99, 123456789_01):
        text = custom_export.format_amount(cents)
        assert "," not in text and "$" not in text and "E" not in text.upper()


def test_format_date_is_mm_dd_yyyy():
    assert custom_export.format_date("2025-01-02") == "01/02/2025"
    assert custom_export.format_date("2025-12-31") == "12/31/2025"
    with pytest.raises(ValueError):
        custom_export.format_date("12/31/2025")       # not the storage shape


# --------------------------------------------------------------------------
# What gets a record
# --------------------------------------------------------------------------

def test_only_refnum_bearing_non_computed_items_are_exported(conn, tax_report):
    records = custom_export.txf_records(conn, tax_report["id"])
    assert [r["name"] for r in records] == [WAGES_A, WAGES_B, INTEREST]
    assert [r["amount"] for r in records] == [40000_00, 1250_50, 312_45]
    assert SUBTOTAL not in [r["name"] for r in records]   # no refnum
    assert TOTAL not in [r["name"] for r in records]      # COMPUTED


def test_copies_share_a_refnum_and_number_their_own_lines(conn, tax_report):
    records = custom_export.txf_records(conn, tax_report["id"])
    assert [(r["refnum"], r["copy"], r["line"]) for r in records] == [
        (9001, 1, 1), (9001, 2, 1), (9002, 1, 1)]

    # A second line on employer one numbers within ITS pair, leaving employer
    # two's record untouched.
    extra = custom.add_item(conn, tax_report["id"], "TX:wages ANON Corp bonus",
                            "SOSC", txf_refnum=9001, txf_copy=1)
    custom.set_item_categories(
        conn, extra, [ledger.resolve_category(conn, "Wages:ANON Corp")])
    records = custom_export.txf_records(conn, tax_report["id"])
    assert [(r["refnum"], r["copy"], r["line"]) for r in records] == [
        (9001, 1, 1), (9001, 2, 1), (9002, 1, 1), (9001, 1, 2)]


def test_the_file_is_a_txf_v042_header_and_one_record_per_item(conn, tax_report):
    text = custom_export.txf_text(conn, tax_report["id"],
                                  export_date="2026-04-15")
    assert text.startswith("V042\r\nAMammon\r\nD04/15/2026\r\n^\r\n")
    assert text.endswith("^\r\n")
    assert text.count("\r\nTS\r\n") == 3              # three summary records
    assert "\r\nTD\r\n" not in text                   # no detail records yet

    lines = text.split("\r\n")
    assert "\n" not in text.replace("\r\n", "")       # no bare LF anywhere
    assert lines.count("^") == 4                      # header + three records
    assert "N9001" in lines and "N9002" in lines
    assert "N9003" not in lines                       # the COMPUTED refnum
    assert "$40000.00" in lines and "$1250.50" in lines and "$312.45" in lines
    assert "$41562.95" not in lines                   # the COMPUTED total
    assert not any(l.startswith("$") and ("," in l or "$" in l[1:])
                   for l in lines)


def test_export_writes_the_file_and_counts_its_records(conn, tax_report, tmp_path):
    path = tmp_path / "taxes.txf"
    count = custom_export.export_txf_to(conn, tax_report["id"], path,
                                        export_date="2026-04-15")
    assert count == 3
    raw = path.read_bytes()
    assert raw.startswith(b"V042\r\n")
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0   # no CRCRLF, no bare LF
    assert raw.decode("utf-8") == custom_export.txf_text(
        conn, tax_report["id"], export_date="2026-04-15")


def test_a_report_with_no_refnums_writes_an_empty_but_valid_file(conn, tmp_path):
    """A tithing report has no tax lines at all. Refusing to write would tell
    the user less than a file he can open and see is empty."""
    rid = custom.create_report(conn, "Tithing", range_kind="fixed",
                               range_start=START, range_end=END)
    custom.add_item(conn, rid, "TI:wages", "SOSC")
    path = tmp_path / "empty.txf"
    assert custom_export.export_txf_to(conn, rid, path,
                                       export_date="2026-04-15") == 0
    # read_bytes, not read_text: universal newlines would hide the CRLF.
    assert path.read_bytes() == b"V042\r\nAMammon\r\nD04/15/2026\r\n^\r\n"


def test_the_export_follows_the_range_it_is_given(conn, tax_report):
    """The definition is stored once and re-pointed; the export is just another
    reader of ``evaluate``, so an explicit range overrides the binding."""
    records = custom_export.txf_records(conn, tax_report["id"],
                                        "2025-01-01", "2025-03-31")
    assert [r["amount"] for r in records] == [40000_00, 0, 0]

    records = custom_export.txf_records(conn, tax_report["id"],
                                        "2024-01-01", "2024-12-31")
    assert [r["amount"] for r in records] == [0, 0, 0]


def test_a_negative_line_exports_as_a_negative_number(conn, tax_report):
    """Sign is presentation and applies before the export sees the number."""
    custom.update_item(conn, tax_report["ids"][INTEREST], sign=-1)
    records = custom_export.txf_records(conn, tax_report["id"])
    assert records[-1]["amount"] == -312_45
    text = custom_export.txf_text(conn, tax_report["id"],
                                  export_date="2026-04-15")
    assert "$-312.45" in text.split("\r\n")


# --------------------------------------------------------------------------
# One item, several properties: the per-tag breakdown becomes several COPIES
# --------------------------------------------------------------------------

MAPLE = "Maple Street"
OAK = "Oak Avenue"


def test_a_broken_down_item_exports_one_record_per_tag(conn, tax_report):
    """Several Schedule E properties are several COPIES of one form line, which
    is the whole reason the breakdown carries a copy number of its own."""
    rid = tax_report["id"]
    account = ledger.create_account(conn, "Rentals", "checking",
                                    opening_balance=0,
                                    opening_date="2024-12-01")
    cat = ledger.resolve_category(conn, "Rental:Property tax")
    for date, amount, tags in (("2025-02-01", -1200_00, [MAPLE]),
                               ("2025-02-02", -800_00, [OAK]),
                               ("2025-02-03", -150_00, [])):
        txn = ledger.add_transaction(conn, account, date, amount,
                                     category_id=cat)
        if tags:
            ledger.set_tags(conn, txn, tags)
    item = custom.add_item(conn, rid, "TX:property tax", "SOSC",
                           txf_refnum=9100, txf_copy=1, txf_format=1,
                           options={"break_by_tag": True})
    custom.set_item_categories(conn, item, [cat])

    records = [r for r in custom_export.txf_records(conn, rid)
               if r["refnum"] == 9100]
    assert [(r["copy"], r["line"], r["label"], r["amount"])
            for r in records] == [(1, 1, MAPLE, -1200_00),
                                  (2, 1, OAK, -800_00),
                                  (3, 1, custom.UNTAGGED_LABEL, -150_00)]
    # The item's own total is NOT also exported: that would bill it twice.
    assert sum(r["amount"] for r in records) == \
        custom.evaluate(conn, rid).amount("TX:property tax")

    # An explicit list pins each property to a copy number that does not move
    # from one filing year to the next.
    custom.update_item(conn, item, options={"break_by_tag": [OAK, MAPLE]})
    records = [r for r in custom_export.txf_records(conn, rid)
               if r["refnum"] == 9100]
    assert [(r["copy"], r["label"]) for r in records] == [
        (1, OAK), (2, MAPLE), (3, custom.UNTAGGED_LABEL)]

    # And the rest of the report is untouched by any of it.
    others = [r for r in custom_export.txf_records(conn, rid)
              if r["refnum"] != 9100]
    assert [(r["refnum"], r["copy"], r["line"]) for r in others] == [
        (9001, 1, 1), (9001, 2, 1), (9002, 1, 1)]
