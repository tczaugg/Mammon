"""Tests for mammon.reports.report_defs: year-definition files (SRD 5.9).

Phase 3 is the domain layer for the two operations a tax report lives or dies
by. **Create Tax Report** turns a form's published line list into a report with
every line present and nothing assigned -- all zeros, and a to-do list.
**Update Report** builds next year's report from next year's line list and
carries the user's category and account assignments across, because re-deciding
thirty assignments every January is how a tax report stops being used.

The life-cycle test below is the acceptance bar. It pins the four things that
can go wrong when a form is renumbered, in one run over two years:

* a line that kept its meaning and changed its NUMBER carries its selections;
* a line that is genuinely new says so, rather than inheriting something;
* a line that DISAPPEARED is reported with its categories spelled out by name,
  because the user's real question is "where do those go now?"; and
* a line whose KIND changed copies nothing, because a category selection on a
  line that no longer sums categories is not stale, it is meaningless.

Plus the property that makes all of it safe to run: the source report -- last
year's filed return -- comes out byte for byte unchanged.

All data here is synthetic: a made-up form FORM-X with made-up line numbers and
made-up TXF reference numbers, and two anonymous accounts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mammon import db, ledger, paths
from mammon.reports import custom, report_defs
from mammon.tests import fresh_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEF_2025 = FIXTURES / "example_formx_2025.json"
DEF_2026 = FIXTURES / "example_formx_2026.json"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "report_defs.db")
    yield c
    c.close()


@pytest.fixture
def ledger_data(conn):
    """Two anonymous accounts, four categories, and money on both sides of the
    2025 boundary so the range is doing real work."""
    checking = ledger.create_account(conn, "ANON Checking", "checking",
                                     opening_balance=1000_00,
                                     opening_date="2024-01-01")
    savings = ledger.create_account(conn, "ANON Savings", "savings",
                                    opening_balance=500_00,
                                    opening_date="2024-01-01")
    wages = ledger.resolve_category(conn, "Wages")
    interest = ledger.resolve_category(conn, "Interest")
    charity = ledger.resolve_category(conn, "Charity")
    property_tax = ledger.resolve_category(conn, "Property Tax")

    ledger.add_transaction(conn, checking, "2024-06-01", 9999_00,
                           category_id=wages)          # before the range
    ledger.add_transaction(conn, checking, "2025-02-15", 4000_00,
                           category_id=wages)
    ledger.add_transaction(conn, checking, "2025-08-15", 2000_00,
                           category_id=wages)
    ledger.add_transaction(conn, savings, "2025-07-01", 125_00,
                           category_id=interest)
    ledger.add_transaction(conn, checking, "2025-09-01", -800_00,
                           category_id=property_tax)
    ledger.add_transaction(conn, checking, "2025-12-01", -300_00,
                           category_id=charity)
    ledger.add_transaction(conn, checking, "2026-03-01", 7000_00,
                           category_id=wages)          # after the range

    return {"checking": checking, "savings": savings, "wages": wages,
            "interest": interest, "charity": charity,
            "property_tax": property_tax}


def _snapshot(conn, report_id):
    """Everything persisted about a report, for the never-touch-the-source
    assertion: the header, every item column, and every selection row."""
    header = dict(conn.execute(
        "SELECT * FROM report_defs WHERE id = ?", (report_id,)).fetchone())
    items = []
    for item in custom.list_items(conn, report_id):
        items.append((
            tuple(dict(conn.execute("SELECT * FROM report_items WHERE id = ?",
                                    (item.id,)).fetchone()).items()),
            tuple(custom.item_categories(conn, item.id)),
            tuple(custom.item_accounts(conn, item.id)),
        ))
    return (tuple(sorted(header.items())), tuple(items))


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def test_load_parses_the_schema_the_design_specifies():
    defn = report_defs.load_definition(DEF_2025)
    assert defn.id == "example-formx-2025"
    assert defn.family == "example-formx"
    assert defn.kind == "tax"
    assert defn.year == 2025
    assert defn.default_range == {"kind": "calendar_year", "year": 2025}
    assert [i.name for i in defn.items] == [
        "FX:line 1", "FX:line 6b", "FX:line 9", "FX:line 11", "FX:line 20"]

    line1 = defn.item("FX:line 1")
    assert (line1.kind, line1.sign, line1.group_label, line1.seq) == \
        ("SOSC", 1, "Income", 10)
    assert (line1.txf_refnum, line1.txf_copy, line1.txf_format) == (9001, 1, 1)
    # Silence is not the same as False: the migration needs to tell "the form
    # says so" from "the file did not say".
    assert line1.tag_enabled is None
    assert defn.item("FX:line 6b").tag_enabled is True


def test_the_three_migrated_from_forms_normalize_to_one_shape():
    defn = report_defs.load_definition(DEF_2026)
    assert defn.item("FX:line 7").migrated_from == (None, "FX:line 6b")
    assert defn.item("FX:line 20").migrated_from == \
        ("example-formx-2025", "FX:line 20")
    assert defn.item("FX:line 12").migrated_from is None


def test_an_unreadable_definition_is_refused_loudly(tmp_path):
    future = tmp_path / "future.json"
    future.write_text(json.dumps({"version": 99, "id": "x", "items": []}),
                      encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        report_defs.load_definition(future)

    bad_kind = tmp_path / "bad.json"
    bad_kind.write_text(json.dumps({
        "version": 1, "id": "bad", "year": 2025,
        "items": [{"name": "a", "kind": "NOT_A_KIND"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="NOT_A_KIND"):
        report_defs.load_definition(bad_kind)


def test_yaml_without_pyyaml_explains_itself(tmp_path, monkeypatch):
    """PyYAML is not a dependency, so a .yaml definition must say what to do
    rather than die on an ImportError."""
    doc = tmp_path / "y.yaml"
    doc.write_text("version: 1\nid: y\n", encoding="utf-8")
    import builtins
    real_import = builtins.__import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no module named yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    with pytest.raises(ValueError, match="PyYAML"):
        report_defs.load_definition(doc)


def test_the_user_root_is_searched_before_the_shipped_one(tmp_path, monkeypatch):
    monkeypatch.setenv("MAMMON_REPORT_DEFS_DIR", str(tmp_path))
    assert report_defs.search_roots() == [
        tmp_path, paths.install_root() / "mammon" / "report_defs"]

    (tmp_path / "mine.json").write_text(json.dumps({
        "version": 1, "id": "mine", "year": 2025,
        "items": [{"name": "L1", "kind": "SOSC"}]}), encoding="utf-8")
    assert report_defs.find_definition_file("mine") == tmp_path / "mine.json"
    assert report_defs.load_definition("mine").id == "mine"

    with pytest.raises(FileNotFoundError):
        report_defs.find_definition_file("nobody-has-this")


# --------------------------------------------------------------------------
# The life cycle
# --------------------------------------------------------------------------

def test_definition_life_cycle(conn, ledger_data, tmp_path, monkeypatch):
    d = ledger_data

    # -- load the 2025 definition and Create Tax Report ---------------------
    defn_2025 = report_defs.load_definition(DEF_2025)
    rid_2025 = report_defs.create_from_definition(conn, defn_2025,
                                                  name="Form X 2025")

    report = custom.get_report(conn, rid_2025)
    assert report.kind == "tax"
    assert report.definition_id == "example-formx-2025"
    assert (report.range_kind, report.range_year) == ("calendar_year", 2025)

    items = {i.name: i for i in custom.list_items(conn, rid_2025)}
    assert list(items) == [i.name for i in defn_2025.items]     # in seq order
    assert items["FX:line 9"].sign == -1
    assert items["FX:line 6b"].tag_enabled == 1
    assert items["FX:line 1"].txf_refnum == 9001

    # Every item exists with ZERO selections, so the report is all zeros and
    # every line is on the to-do list. (custom.Coverage answers a different
    # question -- what fell OUT of the money the report was pointed at -- and is
    # empty by construction when nothing is selected at all; the unassigned
    # lines are what the panel shows for a fresh report.)
    for item in items.values():
        assert custom.item_categories(conn, item.id) == []
        assert custom.item_accounts(conn, item.id) == []
    assert report_defs.unassigned_items(conn, rid_2025) == list(items)
    fresh = custom.evaluate(conn, rid_2025)
    assert [row.amount for row in fresh.rows] == [0, 0, 0, 0, 0]
    assert fresh.coverage.clean

    # -- assign categories and accounts to each line ------------------------
    custom.set_item_categories(conn, items["FX:line 1"].id, [d["wages"]])
    custom.set_item_categories(conn, items["FX:line 6b"].id, [d["interest"]])
    custom.set_item_categories(conn, items["FX:line 9"].id, [d["charity"]])
    custom.set_item_categories(conn, items["FX:line 11"].id,
                               [(d["property_tax"], 1)])
    custom.set_item_accounts(conn, items["FX:line 20"].id, [d["savings"]])
    assert report_defs.unassigned_items(conn, rid_2025) == []

    # -- evaluate and assert cents -----------------------------------------
    ev = custom.evaluate(conn, rid_2025)
    assert (ev.start, ev.end) == ("2025-01-01", "2025-12-31")
    assert ev.amount("FX:line 1") == 6000_00         # not the 2024 or 2026 pay
    assert ev.amount("FX:line 6b") == 125_00
    assert ev.amount("FX:line 9") == 300_00          # sign -1 shows it positive
    assert ev.amount("FX:line 11") == 800_00
    assert ev.amount("FX:line 20") == 625_00         # savings at 2025-12-31

    before = _snapshot(conn, rid_2025)

    # -- load 2026 and Update Report ---------------------------------------
    # 2026 renumbers 6b -> 7, adds line 12, drops line 9, and turns line 11
    # from a category sum into a disclosed balance.
    rid_2026, result = report_defs.update_report(conn, rid_2025, DEF_2026,
                                                 name="Form X 2026")

    new_items = {i.name: i for i in custom.list_items(conn, rid_2026)}
    assert set(new_items) == {"FX:line 1", "FX:line 7", "FX:line 11",
                              "FX:line 12", "FX:line 20"}

    # the renumbered line carried its categories
    carried = {c.name: c for c in result.carried}
    assert set(carried) == {"FX:line 1", "FX:line 7", "FX:line 20"}
    assert carried["FX:line 7"].from_name == "FX:line 6b"
    assert carried["FX:line 7"].n_categories == 1
    assert custom.item_categories(conn, new_items["FX:line 7"].id) == \
        [(d["interest"], 0)]
    assert carried["FX:line 20"].n_accounts == 1
    assert custom.item_accounts(conn, new_items["FX:line 20"].id) == [d["savings"]]

    # the new line is in new_lines, and nothing was inherited into it
    assert result.new_lines == ("FX:line 12",)
    assert custom.item_categories(conn, new_items["FX:line 12"].id) == []

    # the disappeared line is dropped, WITH its category names spelled out
    assert [dr.name for dr in result.dropped] == ["FX:line 9"]
    assert result.dropped[0].categories == ("Charity",)
    assert result.dropped[0].label == "Gifts to charity"

    # the kind change copied nothing
    assert len(result.kind_changed) == 1
    changed = result.kind_changed[0]
    assert (changed.name, changed.from_name) == ("FX:line 11", "FX:line 11")
    assert (changed.from_kind, changed.to_kind) == ("SOSC", "EDAB")
    assert custom.item_categories(conn, new_items["FX:line 11"].id) == []
    assert custom.item_accounts(conn, new_items["FX:line 11"].id) == []
    assert result.unresolved == ()
    assert not result.clean          # dropped + kind_changed need the user

    # -- last year's filed return is untouched ------------------------------
    assert _snapshot(conn, rid_2025) == before
    assert custom.evaluate(conn, rid_2025).amount("FX:line 1") == 6000_00

    # -- a migrated_from pointing at nothing is REPORTED, never raised ------
    monkeypatch.setenv("MAMMON_REPORT_DEFS_DIR", str(tmp_path))
    (tmp_path / "example-formx-2027.json").write_text(json.dumps({
        "version": 1, "id": "example-formx-2027", "family": "example-formx",
        "title": "Example Form X, year 2027 (synthetic)", "kind": "tax",
        "year": 2027,
        "items": [{"name": "FX:line 1", "kind": "SOSC", "seq": 10,
                   "migrated_from": "FX:line 99"}],
    }), encoding="utf-8")

    rid_2027, result_27 = report_defs.update_report(
        conn, rid_2026, "example-formx-2027", name="Form X 2027")
    assert [u.ref for u in result_27.unresolved] == ["FX:line 99"]
    assert result_27.unresolved[0].name == "FX:line 1"
    assert custom.item_categories(
        conn, custom.list_items(conn, rid_2027)[0].id) == []


# --------------------------------------------------------------------------
# Create, in isolation
# --------------------------------------------------------------------------

def test_create_honours_a_range_override(conn):
    rid = report_defs.create_from_definition(
        conn, DEF_2025, name="Form X, fiscal",
        range_override={"kind": "fixed", "start": "2025-07-01",
                        "end": "2026-06-30"})
    report = custom.get_report(conn, rid)
    assert (report.range_kind, report.range_start, report.range_end) == \
        ("fixed", "2025-07-01", "2026-06-30")


def test_a_duplicate_report_name_is_refused(conn):
    report_defs.create_from_definition(conn, DEF_2025, name="Form X 2025")
    with pytest.raises(ValueError):
        report_defs.create_from_definition(conn, DEF_2025, name="Form X 2025")


def test_a_half_built_report_is_never_left_behind(conn, tmp_path):
    """A form missing a line looks complete and silently under-reports, so a
    failure part-way through rolls the whole report back."""
    doc = tmp_path / "dup.json"
    doc.write_text(json.dumps({
        "version": 1, "id": "dup", "year": 2025,
        "items": [{"name": "L1", "kind": "SOSC", "seq": 10},
                  {"name": "L2", "kind": "NOPE", "seq": 20}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        report_defs.load_definition(doc)

    # The same protection, from a failure the loader cannot see: a tag name
    # already taken ledger-wide.
    taken = report_defs.create_from_definition(conn, DEF_2025,
                                               name="Form X 2025")
    clash = tmp_path / "clash.json"
    clash.write_text(json.dumps({
        "version": 1, "id": "clash", "year": 2026,
        "items": [{"name": "FX:line 6b", "kind": "SOSC", "seq": 10,
                   "tag_enabled": True}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        report_defs.create_from_definition(conn, clash, name="Form X clash")
    assert [r.id for r in custom.list_reports(conn)] == [taken]


# --------------------------------------------------------------------------
# The YAML path, exercised by the definition Mammon actually ships
# --------------------------------------------------------------------------
# `mammon/report_defs/quicken_tax_lines.yaml` is the first definition to ship
# in the install's own root AND the first written in YAML, so these tests hold
# both of those down: a loader that only ever reads test fixtures by explicit
# path has never proved that the bundled root is searched, and a YAML branch
# that no test reaches is a branch that silently stopped working.

TAX_LINES_YAML = (paths.install_root() / "mammon" / "report_defs"
                  / "quicken_tax_lines.yaml")


def test_pyyaml_is_a_required_dependency():
    """Not a skip: PyYAML is in requirements.txt and pyproject.toml, and a
    shipped definition is written in YAML. An install that cannot read it is
    broken, so its absence has to FAIL here rather than quietly pass."""
    import yaml

    assert yaml.safe_load("a: 1\nb: [2, 3]\n") == {"a": 1, "b": [2, 3]}


def test_the_shipped_tax_lines_are_found_in_the_bundled_root(tmp_path,
                                                             monkeypatch):
    """Found by id, through the ordinary two-root search, with the user's own
    (here empty) directory shadowing nothing."""
    monkeypatch.setenv("MAMMON_REPORT_DEFS_DIR", str(tmp_path))

    assert TAX_LINES_YAML.is_file()
    assert TAX_LINES_YAML in report_defs.list_definitions()
    assert report_defs.find_definition_file("quicken_tax_lines") == \
        TAX_LINES_YAML

    defn = report_defs.load_definition("quicken_tax_lines")   # by id, not path
    assert (defn.id, defn.kind, defn.year) == ("quicken_tax_lines", "tax", 2025)
    assert defn.default_range == {"kind": "calendar_year", "year": 2025}
    assert defn.source == TAX_LINES_YAML
    assert defn.title


def test_every_shipped_tax_line_round_trips_through_the_parser():
    """Every item in the file, not just the handful the life-cycle test names:
    a line that parses to something different from what the file says is a
    number filed on the wrong tax line."""
    import yaml

    raw = yaml.safe_load(TAX_LINES_YAML.read_text(encoding="utf-8"))
    defn = report_defs.load_definition(TAX_LINES_YAML)
    assert len(defn.items) == len(raw["items"]) > 0

    for index, (entry, item) in enumerate(zip(raw["items"], defn.items)):
        again = report_defs._parse_item(entry, TAX_LINES_YAML.name, index)
        assert again == item                      # re-parse is identical
        assert item.kind in custom.ALL_KINDS
        assert item.sign in (1, -1)               # never left to the default
        assert item.label and item.group_label
        assert item.seq is not None
        if item.txf_refnum is not None:           # omitted, never guessed
            assert item.txf_refnum > 0
            assert item.txf_copy == 1
        if item.kind == "RGAIN":
            assert item.options["term"] in custom.RGAIN_TERMS

    names = [i.name for i in defn.items]
    assert len(set(names)) == len(names)
    assert [i.seq for i in defn.items] == sorted(i.seq for i in defn.items)
    # Grouped by form, and at least the common Quicken schedules are present.
    groups = {i.group_label for i in defn.items}
    assert {"W-2", "Schedule A", "Schedule B", "Schedule C", "Schedule D",
            "Schedule E", "Form 1040"} <= groups
    # A checked-in file carries tax line names and reference numbers only.
    assert not any(i.tag_enabled for i in defn.items)


def test_the_shipped_tax_lines_create_and_evaluate(conn, ledger_data):
    """Load to evaluate, over the same seeded ledger the Form X tests use."""
    d = ledger_data

    defn = report_defs.load_definition(TAX_LINES_YAML)
    rid = report_defs.create_from_definition(conn, defn,
                                             name="Tax line items 2025")

    report = custom.get_report(conn, rid)
    assert report.kind == "tax"
    assert report.definition_id == "quicken_tax_lines"
    assert (report.range_kind, report.range_year) == ("calendar_year", 2025)

    items = {i.name: i for i in custom.list_items(conn, rid)}
    assert list(items) == [i.name for i in defn.items]          # in seq order
    assert items["W-2:Salary"].sign == 1
    assert items["W-2:Salary"].txf_refnum == 460                # TXF v042
    assert items["Schedule B:Interest income"].txf_refnum == 287
    assert items["Schedule A:Cash charity contributions"].sign == -1
    assert items["Schedule D:Long-term gain/loss - security"].kind == "RGAIN"
    # Lines whose refnum could not be established ship without one rather than
    # with a guess.
    assert items["Schedule C:Gross receipts or sales"].txf_refnum is None

    # Created with NO selections, so it is all zeros and every line is on the
    # to-do list -- including the two RGAIN lines, which have no lots to find.
    fresh = custom.evaluate(conn, rid)
    assert {row.amount for row in fresh.rows} == {0}
    assert report_defs.unassigned_items(conn, rid) == list(items)

    custom.set_item_categories(conn, items["W-2:Salary"].id, [d["wages"]])
    custom.set_item_categories(conn, items["Schedule B:Interest income"].id,
                               [d["interest"]])
    custom.set_item_categories(
        conn, items["Schedule A:Cash charity contributions"].id, [d["charity"]])
    custom.set_item_categories(conn, items["Schedule A:Real estate tax"].id,
                               [(d["property_tax"], 1)])

    ev = custom.evaluate(conn, rid)
    assert (ev.start, ev.end) == ("2025-01-01", "2025-12-31")
    assert ev.amount("W-2:Salary") == 6000_00        # not the 2024 or 2026 pay
    assert ev.amount("Schedule B:Interest income") == 125_00
    assert ev.amount("Schedule A:Cash charity contributions") == 300_00
    assert ev.amount("Schedule A:Real estate tax") == 800_00   # sign -1: shown
    # The wages did not also land on a line nobody assigned them to.
    assert ev.amount("Schedule C:Gross receipts or sales") == 0
    assert ev.amount("Schedule E:Rents received") == 0


# --- packaging: the bundled root has to survive an install -------------------
#
# The shipped definitions live in mammon/report_defs/, which has NO __init__.py,
# so setuptools' packages.find does not see it: without an explicit package-data
# glob a wheel or sdist installs an app whose bundled tax lines simply are not
# there. That is a broken install, not a configuration (CLAUDE.md), and it fails
# silently -- list_definitions() just returns one fewer path. These two tests pin
# both halves: the definition is reachable through the ordinary code path, and
# the packaging declaration that keeps it reachable is still present.

def test_the_bundled_definition_loads_its_items_through_list_definitions(
        tmp_path, monkeypatch):
    """No path passed in anywhere: the file is discovered in the shipped root
    and parsed into real items."""
    monkeypatch.setenv("MAMMON_REPORT_DEFS_DIR", str(tmp_path))   # empty user root

    found = [p for p in report_defs.list_definitions()
             if p.stem == "quicken_tax_lines"]
    assert found == [TAX_LINES_YAML]

    defn = report_defs.load_definition(found[0].stem)
    assert defn.items, "the shipped definition parsed to zero items"
    names = [item.name for item in defn.items]
    assert len(set(names)) == len(names)          # no duplicate line names
    assert all(item.kind for item in defn.items)


def test_packaging_ships_the_bundled_report_definitions():
    """Every accepted definition form in the shipped root is covered by a
    package-data glob, so an installed copy keeps them."""
    import fnmatch
    import tomllib

    root = paths.install_root()
    declared = tomllib.loads((root / "pyproject.toml").read_text(
        encoding="utf-8"))["tool"]["setuptools"]["package-data"]["mammon"]

    bundled = [p for p in (root / "mammon" / "report_defs").iterdir()
               if p.is_file() and p.suffix.lower() in report_defs.SUFFIXES]
    assert bundled, "nothing bundled to ship"

    for path in bundled:
        rel = path.relative_to(root / "mammon").as_posix()
        assert any(fnmatch.fnmatch(rel, pattern) for pattern in declared), \
            f"{rel} is not covered by package-data {declared}"

    # And every suffix the loader accepts, not just the ones present today.
    for suffix in report_defs.SUFFIXES:
        rel = f"report_defs/anything{suffix}"
        assert any(fnmatch.fnmatch(rel, pattern) for pattern in declared), \
            f"a definition named {rel} would not be installed"
