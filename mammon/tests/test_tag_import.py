"""Quicken tags survive the trip through QIF import.

The bug this guards: ``record.clean_category`` split ``Category/Tag`` and threw
the tag half away, and ``!Type:Tag`` sat in the parser's "metadata we do not
consume" bucket. A real 40-year ledger carried 400 taggings across nine years
and every one of them arrived as an untagged transaction -- silently, because
stripping the suffix left a perfectly valid category behind. Nothing looked
wrong until the By Tag report came up empty.

Two shapes matter and they are NOT the same shape: a tag on the ``L`` line
describes the whole transaction, while a tag on an ``S`` split leg describes
only that leg. Quicken echoes the first leg's category (tag included) up onto
the ``L`` line of a split, so the row-level reading of that echo has to be
suppressed or one leg's project silently claims the entire payment.
"""
import os
import tempfile

import pytest

from mammon import db, ledger
from mammon.importers import import_file
from mammon.importers.record import clean_category, split_category_tag


@pytest.fixture
def conn():
    d = tempfile.mkdtemp()
    return db.init_db(os.path.join(d, "t.db"))


def _import(conn, qif, name="Mammon_2018.QIF"):
    d = tempfile.mkdtemp()
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(qif)
    return import_file(conn, path, account="Checking")


def _txn(conn, payee):
    return conn.execute(
        "SELECT * FROM transactions WHERE payee=?", (payee,)).fetchone()


def _legs(conn, txn_id):
    return [(r["amount"], r["name"]) for r in conn.execute(
        "SELECT s.amount, g.name FROM splits s LEFT JOIN tags g ON g.id = s.tag_id "
        "WHERE s.transaction_id=? ORDER BY s.id", (txn_id,))]


# --- the field splitter ----------------------------------------------------
def test_split_category_tag_separates_the_two_halves():
    assert split_category_tag("Business:Research/Rig 8") == ("Business:Research", "Rig 8")
    assert split_category_tag("Groceries") == ("Groceries", "")
    assert split_category_tag("  A:B / Ski Trip ") == ("A:B", "Ski Trip")
    assert split_category_tag("") == ("", "")


def test_only_the_first_slash_separates():
    """Quicken's nested-class form is ``Category/Tag:Subtag``, so everything
    after the FIRST slash belongs to the tag. Splitting on the last one would
    move part of the tag into the category path."""
    assert split_category_tag("Cat/Tag:Sub") == ("Cat", "Tag:Sub")


def test_clean_category_still_answers_only_the_category():
    """Its callers are unchanged; it simply routes through the splitter now."""
    assert clean_category("Business:Research/Rig 8") == "Business:Research"


# --- transaction-level tags ------------------------------------------------
def test_a_tag_on_the_l_line_tags_the_transaction(conn):
    """THE regression: this arrived untagged, with the category intact, so
    nothing downstream could tell the tag had ever been there."""
    _import(conn, "!Type:Bank\nD1/15'18\nT-100.00\nPParts Co\n"
                  "LBusiness:Research/Rig 8\n^\n")
    t = _txn(conn, "Parts Co")
    assert t["category_id"] is not None
    assert ledger.get_tags(conn, t["id"]) == ["Rig 8"]
    assert t["tag"] == "Rig 8"          # the comma-joined cache stays in step


def test_tag_names_keep_their_spaces(conn):
    """Every tag in the source ledger has a space in it -- 'Rig 8', 'Escaping
    the Virus'. parse_tags splits on commas ONLY, and the importer must not
    tokenize on whitespace on the way in either."""
    _import(conn, "!Type:Bank\nD3/01'21\nT-40.00\nPBookshop\n"
                  "LSupplies/Escaping the Virus\n^\n")
    assert ledger.get_tags(conn, _txn(conn, "Bookshop")["id"]) == ["Escaping the Virus"]


def test_a_bracketed_transfer_is_not_read_as_a_tag(conn):
    """``L[Savings]`` is a transfer target. An account name may contain a slash,
    and treating it as a category/tag pair would both invent a tag and destroy
    the account name."""
    _import(conn, "!Type:Bank\nD1/20'18\nT-500.00\nPMove\nL[Savings]\n^\n")
    t = _txn(conn, "Move")
    assert t["transfer_account_id"] is not None
    assert ledger.get_tags(conn, t["id"]) == []


# --- split-leg tags --------------------------------------------------------
def test_each_split_leg_keeps_its_own_tag(conn):
    """One parts order across two projects. Per-leg tags are the whole point:
    the amounts differ, so crediting both tags with the full $250 would
    overstate each of them in a by-tag spending report."""
    _import(conn, "!Type:Bank\nD2/15'18\nT-250.00\nPBig Order\n"
                  "LBusiness:Research/Rig 8\n"
                  "SBusiness:Research/Rig 8\n$-100.00\n"
                  "SBusiness:Research/Rig 9\n$-150.00\n^\n")
    t = _txn(conn, "Big Order")
    assert _legs(conn, t["id"]) == [(-100_00, "Rig 8"), (-150_00, "Rig 9")]


def test_the_echoed_l_tag_does_not_tag_the_whole_split(conn):
    """Quicken repeats the first leg's category, tag and all, on the L line of a
    split. Reading it at row level would tag the entire $250 'Rig 8' on top of
    the correctly-tagged legs, double-counting it against itself."""
    _import(conn, "!Type:Bank\nD2/15'18\nT-250.00\nPBig Order\n"
                  "LBusiness:Research/Rig 8\n"
                  "SBusiness:Research/Rig 8\n$-100.00\n"
                  "SBusiness:Research/Rig 9\n$-150.00\n^\n")
    t = _txn(conn, "Big Order")
    assert ledger.get_tags(conn, t["id"]) == []
    assert t["tag"] is None


def test_an_untagged_leg_stays_untagged(conn):
    """A split may be partly tagged; the untagged legs must not inherit a
    neighbour's tag, or untagged money disappears into a project."""
    _import(conn, "!Type:Bank\nD2/20'18\nT-300.00\nPMixed\n"
                  "SBusiness:Research/Rig 8\n$-100.00\n"
                  "SOffice\n$-200.00\n^\n")
    assert _legs(conn, _txn(conn, "Mixed")["id"]) == [(-100_00, "Rig 8"), (-200_00, None)]


# --- the !Type:Tag master --------------------------------------------------
def test_the_tag_master_arrives_with_its_descriptions(conn):
    """A tag defined but never applied still exists, and the description is the
    only place its meaning is written down. Both were dropped before."""
    _import(conn, "!Type:Tag\nNRig 8\nDEighth build\n^\n"
                  "NUnused\nDDefined, never applied\n^\n"
                  "!Type:Bank\nD1/15'18\nT-10.00\nPx\nLOffice\n^\n")
    rows = {r["name"]: r["description"] for r in
            conn.execute("SELECT name, description FROM tags")}
    assert rows == {"Rig 8": "Eighth build", "Unused": "Defined, never applied"}


def test_a_description_already_set_is_not_overwritten(conn):
    """Importing an OLDER yearly export must not walk back a description the
    user has since edited -- the yearly files are imported in sequence and each
    one carries the whole master."""
    _import(conn, "!Type:Tag\nNRig 8\nDcurrent\n^\n"
                  "!Type:Bank\nD1/15'18\nT-10.00\nPx\nLOffice\n^\n")
    _import(conn, "!Type:Tag\nNRig 8\nDstale\n^\n"
                  "!Type:Bank\nD1/15'17\nT-11.00\nPy\nLOffice\n^\n",
            name="Mammon_2017.QIF")
    assert conn.execute(
        "SELECT description FROM tags WHERE name='Rig 8'").fetchone()[0] == "current"


def test_the_same_tag_across_years_stays_one_row(conn):
    """tags.name collates NOCASE and the master repeats in every yearly export,
    so 31 files must not yield 31 copies of one tag."""
    for yr, spell in (("18", "Rig 8"), ("19", "rig 8"), ("20", "RIG 8")):
        _import(conn, "!Type:Tag\nN%s\n^\n"
                      "!Type:Bank\nD1/15'%s\nT-10.00\nP%s\nLOffice/%s\n^\n"
                      % (spell, yr, yr, spell),
                name="Mammon_20%s.QIF" % yr)
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transaction_tags").fetchone()[0] == 3


# --- the round trip --------------------------------------------------------
def test_tags_survive_a_qif_round_trip(conn):
    """Export then re-import reproduces every tag: the master with its
    descriptions, a row's own tag, and each split leg's.

    The export used to drop all of it, justified by a docstring claiming "QIF
    cannot carry tags". The spec says otherwise (L is
    "Category/Subcategory/Transfer/Class", S is "Category/Transfer/Class", and
    !Type:Class is a list of N/D pairs), and so do 27 years of Quicken's own
    exports. Fixing only the import half would have left the ledger able to
    read tags it could never write back out."""
    import os
    import tempfile

    from mammon import export

    aid = ledger.create_account(conn, "Checking", "checking")
    cat = ledger.resolve_category(conn, "Business:Research")
    plain = ledger.add_transaction(conn, aid, "2018-01-15", -100_00,
                                   payee="Parts Co", category_id=cat)
    ledger.set_tags(conn, plain, ["Rig 8"])
    split = ledger.add_transaction(conn, aid, "2018-02-15", -250_00, payee="Big Order")
    ledger.set_splits(conn, split, [
        {"category_id": cat, "amount": -100_00, "memo": ""},
        {"category_id": cat, "amount": -150_00, "memo": ""}])
    for leg, name in zip(ledger.get_splits(conn, split), ("Rig 8", "Rig 9")):
        conn.execute("UPDATE splits SET tag_id=? WHERE id=?",
                     (ledger.tag_id(conn, name), leg["id"]))
    conn.execute("UPDATE tags SET description='Eighth build' WHERE name='Rig 8'")
    conn.commit()

    path = os.path.join(tempfile.mkdtemp(), "out.qif")
    export.export_qif(conn, path)
    text = open(path, encoding="utf-8").read()
    assert text.startswith("!Type:Tag\nNRig 8\nDEighth build\n^\n")

    back = db.init_db(os.path.join(tempfile.mkdtemp(), "b.db"))
    import_file(back, path, account="Checking")

    assert {r["name"]: r["description"] for r in
            back.execute("SELECT name, description FROM tags")} == {
        "Rig 8": "Eighth build", "Rig 9": None}
    assert ledger.get_tags(back, _txn(back, "Parts Co")["id"]) == ["Rig 8"]
    assert _legs(back, _txn(back, "Big Order")["id"]) == [
        (-100_00, "Rig 8"), (-150_00, "Rig 9")]


def test_a_transfer_leg_never_takes_a_tag_suffix(conn):
    """``S[Savings]/Rig 8`` would read back as an account literally named
    'Savings]/Rig 8'. A bracketed leg is a transfer target, not a category."""
    from mammon.export import _tagged
    assert _tagged("[Savings]", "Rig 8") == "[Savings]"
    assert _tagged("Business:Research", "Rig 8") == "Business:Research/Rig 8"
    assert _tagged("Business:Research", "") == "Business:Research"
