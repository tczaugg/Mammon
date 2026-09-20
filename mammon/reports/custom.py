"""mammon.reports.custom -- user-defined reports: the domain layer (SRD 5.9).

A SAVED REPORT DEFINITION is a list of named LINE ITEMS, each of one kind, each
carrying its own selection of categories or accounts, evaluated over a date
range into signed integer cents. It is what a tax report is ("1040 line 1z",
"Schedule E line 5"), what a tithing or a giving statement is, and what the
"just show me these six numbers" report is that no fixed report template ever
quite covers.

Three facts drive every shape in this module.

**Definitions live in the DATABASE, not in QSettings.** Saved report FILTER sets
are a display convenience and belong in settings; a report DEFINITION is data
the user owns. It has to ride the ledger's backups and snapshots, survive moving
the ``.db`` to another machine, land in ``export.py``'s dump of every table, and
cascade when a category or an account is deleted. Migration 75 creates the six
tables; this module is the only thing that writes them.

**Selections are keyed by INTEGER ID, never by name.** ``ledger.rename_category``
keeps the id, so a name-keyed selection silently drops a category the moment the
user tidies a name (SRD 5.9c records exactly that failure for the report filter
bar). A tax report whose Schedule E mapping evaporates because a category was
renamed is the single worst outcome this feature can produce, so the mapping is
``report_item_categories.category_id`` and nothing else. ``include_subtree`` is
stored as a FLAG, never as a snapshot of the expanded ids: the expansion runs
fresh in :func:`_selected_category_ids` through
``reports._lines.category_subtree``, so a child category created next year is
included with no edit to the item.

**A category selection carries the picker's three-state meaning.** A parent the
user left PARTIALLY checked is IN the stored id set (SRD 5.9c: "this parent's
own postings, plus the children still ticked"), and must never be filtered out
for having unticked children -- dropping it would prune its whole subtree and
silently zero a line. So the stored ids are taken at face value: each is in the
set, and each subtree-flagged one additionally contributes its descendants.

Kinds implemented here:

``SOSC``
    Signed net of every register LINE whose category is in the item's selection,
    over ``[start, end]``. "Line" means what ``reports._lines.signed_lines``
    means -- an unsplit transaction, or ONE split leg -- so a paycheck's tax leg
    lands on its own category and nothing is double counted. Transfers carry no
    category and therefore never enter an ``SOSC`` item (they will enter only by
    explicit tag, in Phase 2); scheduled pre-entries are excluded.
``EDAB`` / ``SDAB``
    Ending / starting display account balance, summed over the item's selected
    accounts, through ``investments.display_balance`` -- which is the call that
    already knows how to value a cash, investment, crypto or asset account,
    unlike ``ledger.account_balance``. ``SDAB`` is the OPENING balance: it is
    taken as of the day BEFORE ``start``, so that ``SDAB`` plus the flows over
    ``[start, end]`` equals ``EDAB`` exactly. Taking it as of ``start`` itself
    would double-count every transaction dated on the first day of the range.
``HOLDVAL``
    Market value of the item's selected securities inside its selected accounts
    at the end date -- an empty security selection means every symbol those
    accounts held that day. The share count comes from the replay AS OF that
    date, so a position sold in March is not valued in December.
``RGAIN``
    Realized gain booked by sales dated inside ``[start, end]`` over the
    selected accounts, filtered by ``options["term"]`` -- ``"short"``,
    ``"long"``, or ``"all"`` (the default, which also counts lots whose holding
    period is unknown, because dropping them would understate the total).
``NETGAIN``
    ``(EDAB - SDAB) - (money in - money out)``: what the selected accounts
    gained that was not contributed. The flows come from
    ``portfolio.external_flows``, which reconciles ledger transfer legs against
    the ``XIn``/``XOut`` investment rows recording the same movement, so a
    contribution is subtracted once and not twice. A transfer BETWEEN two
    selected accounts is ``+X`` on one and ``-X`` on the other and cancels,
    which is what "net of the set" has to mean.
``COMPUTED``
    Arithmetic over the OTHER items of the same report (below).

**An item's name doubles as a TAG when ``tag_enabled`` is set** (Phase 2). Report
item tags ARE ordinary tags -- ``tags`` / ``transaction_tags`` / ``splits.tag_id``
(migrations 45 and 54) -- and not a parallel vocabulary: a second tag table would
give the user two places to type a name, two things to rename, and would not ride
QIF export. ``tag_enabled`` therefore means only "this name is looked up in
``tags`` at evaluation time"; the tag ROW is created lazily by the Tag Manager or
the register, never by creating an item, so fifty unused 1040 line names do not
litter the tag list. Matching is case-insensitive because ``tags.name`` collates
NOCASE, and the name is validated at save time by
``ledger.validate_report_item_tag_name`` (no comma, no leading ``!``, NOCASE
unique among tag-enabled names ledger-wide).

The unit of evaluation is the LINE, and its EFFECTIVE TAG SET is
``reports._lines.effective_tags`` -- for a split leg the union of the parent's
tags and the leg's own, never the parent's set folded onto every sibling. Three
precedence rules, in this order:

1. **Exclusion always wins.** ``!N`` on a line beats ``N`` on the same line and
   beats the category selection, with no way back in. That is what lets the user
   say "all of Charity except that one reimbursed thing" without a second
   selection mechanism.
2. **Inclusion beats category selection.** ``N`` on a line whose category the
   item did not select pulls it in -- how a one-off posting joins a tax line.
3. **Category selection is the default, and applies to ``SOSC`` only.** On the
   balance and investment kinds there is nothing to override ("include this
   transaction in an end-of-year balance" is not a coherent instruction), so
   ``N``/``!N`` are IGNORED there -- and FLAGGED in
   ``Coverage.ignored_tags`` rather than silently dropped.

Splits compose the way a user expects: tagging the PARENT pulls in every leg at
its own signed amount, tagging ONE LEG pulls in only that leg (a paycheck's tax
leg joins a tax line without dragging the gross), and parent ``N`` plus leg
``!N`` carves that leg out and leaves its siblings in -- because rule 1 is
evaluated on the leg's own effective set, which already contains the parent's
tags. A leg holds at most one ``tag_id`` (migration 54), so a leg cannot be both
``N`` and ``!M``; the answer there is to tag the parent and carve out at the leg.

Transfers carry no category, so an ``SOSC`` item reaches one in one of two ways,
and in both it is **that leg only, at that leg's own sign** (pulling in the
receiving side counts the money once, not twice, and not on the other side):
by an explicit TAG, or by SELECTING the account on the far side -- an item's
``report_item_accounts`` rows, which for this kind mean "transfers whose other
end is here". The second exists because a tax line is often stated net of money
that merely MOVED: W-2 box 1 wages are gross pay less the 401(k) deferral, and
that deferral is a transfer leg of the paycheck with no category at all, so an
item summing categories could not express the figure the form asks for. Matching
on the FAR side keeps it single-sided -- a transfer contributes two rows, and
only the one outside the selected account points at it. If BOTH legs of one
transfer are pulled in, by tag or by selecting both accounts, that is a
guaranteed double count, reported in ``Coverage.transfer_double_counted`` --
named, with the date -- and never silently de-duplicated, because the user may
have meant an external-looking pair and halving a number behind his back is
worse than a warning.

**BREAKDOWN BY TAG is per item and ON BY DEFAULT, with an opt-out.** It splits
what an item ALREADY matched into one SUB-ROW per tag value, plus an explicit
UNTAGGED remainder, plus the item's own TOTAL row -- which stays bit-for-bit
the number the item produced before any breakdown existed. That is the rental
case: one "Property tax" line whose money has to land on a separate Schedule E
copy per property, without maintaining one item per property.

* DEFAULT ON, and DISCOVERED: an item that says nothing subtotals by every tag
  its own lines carry. It used to be opt-in with a typed tag list, and that was
  wrong twice over -- the user had to restate names the data already knows, and
  forgetting one silently dropped a property into ``(untagged)``. There is
  nothing to configure and nothing to keep in step with the ledger.
* The OPT-OUT is ``options["no_tag_breakdown"] = true``: one bare total, the way
  a report read before any of this existed.
* The RESTRICT LIST survives for stored definitions that carry one:
  ``options["break_by_tag"]`` may be ``true`` ("every tag found", which is now
  also the default), or a LIST of tag names to restrict to and ORDER by -- a
  listed tag is emitted even in a year with no activity, so its TXF copy number
  cannot shift under it. The editor no longer writes a list (there is no tag
  box), but a definition file or an older report that does is honoured.
  ``ReportItem.break_by_tag`` reads all of it back as ``None`` (off), ``()``
  (every tag found) or the tuple of names; an explicit ``break_by_tag: false``
  from an older build still reads as off. It rides ``options`` rather than a
  column of its own because the per-kind settings of an item already live
  there, and a stored report written by an older build must keep loading.
* The breakdown NEVER changes SELECTION: a line is bucketed only after the three
  precedence rules above have already let it in, so the total cannot move.
* Which tags are BUCKETS: every tag on the line except ones beginning with ``!``
  (an exclusion marker is not a property) and except THIS item's own inclusion
  tag -- the tag equal to a tag-enabled item's own name is HOW the line got in,
  so bucketing by it would answer "all of it" and call that a property. No
  OTHER item's name is suppressed: that was the reported defect, because in the
  rental shape the property tags ARE the names of the per-property tag-enabled
  items, and suppressing them emptied every sub-row into ``(untagged)``. A tag
  that carries money on this item's lines is a bucket, full stop; a tag the user
  LISTED explicitly is a bucket even when it is the item's own name.
* If nothing is left to bucket by -- no line carried a usable tag -- the item
  emits NO sub-rows rather than a lone ``(untagged)`` row restating its total.
  That is what keeps default-on quiet: only an item whose data actually has tags
  grows sub-rows. An explicit list still emits its rows, zeros included.
* A line carrying TWO breakdown tags is counted ONCE UNDER EACH. Nothing in the
  data could split that money between them, and "first tag wins" would be a
  silent answer to a real ambiguity. The TOTAL row remains the un-duplicated
  sum, so the sub-rows are allowed to over-sum it; that gap IS the report of the
  double tag.
* Each sub-row carries its own ``txf_copy`` -- ``item.txf_copy`` plus the tag's
  0-based position, with the untagged remainder taking the copy after the last
  tag. That is precisely TXF's mechanism for the second and third Schedule E
  property, and ``custom_export`` emits one record per sub-row.
* A ``COMPUTED`` item over broken-down referents computes PER TAG VALUE as well,
  over the UNION of its referents' tag values, so "interest + property tax +
  maintenance" comes out once per property. A referent with no breakdown of its
  own contributes to the TOTAL only and reads as zero in every per-tag cell. A
  brace name still resolves to the referent's total, as it always did.

Sub-rows PRECEDE their total in ``Evaluation.rows`` (the parts, then the sum)
and are flagged ``ReportRow.is_breakdown``; ``Evaluation.by_name`` and
:func:`compare` deliberately see the TOTAL only, so nothing that existed before
the breakdown moves.

``Coverage`` is the panel beside the rows, informational and never a block:
lines claimed by two or more items (with the item names), and -- restricted to
the union of all ``SOSC`` selections -- lines claimed by NO item, which on a tax
report is the first thing to read (an unclaimed deductible line is money left on
the table). Two items claiming one line is ALLOWED and expected: the user ruled
that one category can feed several tax lines, which is exactly why item totals
deliberately do not add up to a grand total (the only meaningful total is a
``COMPUTED`` item naming the lines the user wants added).

**A ``COMPUTED`` item is a RESTRICTED expression, not Python** (Phase 5). Names
in braces, ``+ - * /``, parentheses, integer and decimal literals -- and nothing
else: no ``eval``, no attribute access, no function calls, so a definition file
downloaded from anywhere is data and never code. Arithmetic runs on integer
cents through ``Decimal`` (never a float), and every DIVISION rounds
``ROUND_HALF_UP`` to whole cents at once, which is what makes "a tenth of the
increase" a number the user can check by hand.

A brace name refers to the referent's **presented** ``amount`` -- after its
``sign`` -- because that is the number the user reads off the row he is adding
up. Writing ``{income} + {rental net}`` where both are sign ``-1`` expense-shaped
lines has to give the sum he sees, not the negated raw total.

Names are resolved to item IDS AT SAVE TIME and stored as edges in
``report_item_refs``, for the same reason selections are keyed by id: a rename
must not break a formula, and the dependency graph must be inspectable without
re-parsing every expression. A forward reference (a definition file listing a
total before its parts) resolves as soon as the referent exists, because every
write to a report re-syncs that report's edges. **Cycles are rejected at SAVE and
re-checked at EVALUATE** -- the second check is not redundant: edges and
expressions are ordinary table rows that a hand-written ``UPDATE`` can corrupt,
and the failure mode being prevented is an evaluation that recurses until the
interpreter dies. Both the detection and the evaluation are iterative, and the
error NAMES the loop.

:func:`compare` evaluates ONE definition over several ranges and aligns the rows
by ``seq``. Two things it must not smooth over. ``Coverage`` is computed PER
COLUMN, because a category that was unclaimed in 2023 and claimed in 2024 is
exactly the discrepancy a comparison exists to show. And a cell is marked
INCOMPLETE (``ReportRow.unpriced``) when a price-dependent kind holds a security
with no price on or before that column's end date: ``holding_values_at`` omits
what it cannot price, so the honest report is "unknown", never a confident zero.
The weaker cousin is ``ReportRow.no_data`` -- nothing at all fed the row -- which
is how "this category did not exist that year" is told apart from "it netted
zero".

Money is signed integer cents throughout, negative = money out. ``item.sign``
multiplies the raw signed total for presentation only (a ``-1`` on an expense
line so the report shows it positive); it never changes what was summed.
Export lives next door in ``mammon.reports.custom_export``, which converts to
dollars and to ``MM/DD/YYYY`` at the file boundary and nowhere else.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Iterable, Optional, Sequence

from mammon import ledger, sqldriver
from mammon.reports._lines import (
    Line, category_paths, category_subtree, effective_tags, signed_lines,
    validate_date)

# The ACTIVE driver's exception class, not ``sqlite3``'s: an install with
# sqlcipher3 present opens every ledger through it, and its IntegrityError is a
# separate class that does not inherit from sqlite3's. Catching sqlite3's here
# would let a duplicate-name error escape unconverted on exactly the installs
# the user runs (mammon/sqldriver.py re-exports the right one).
_IntegrityError = sqldriver.IntegrityError

# Every kind the schema admits, all of them evaluated here. The two tuples stay
# separate because the difference is meaningful: a definition file may name a
# kind this build does not evaluate, and the refusal in evaluate() has to say so
# rather than silently reading zero.
ALL_KINDS = ("SOSC", "EDAB", "SDAB", "HOLDVAL", "RGAIN", "NETGAIN", "COMPUTED")
IMPLEMENTED_KINDS = ALL_KINDS

# Kinds whose number depends on a security PRICE, and so can come back
# incomplete when a held symbol has no price on or before the as-of date.
PRICED_KINDS = ("EDAB", "SDAB", "HOLDVAL", "NETGAIN")

# ``options["term"]`` on a RGAIN item. "all" is the default and deliberately
# includes lots whose holding period could not be determined: dropping them
# would understate the total silently, which is the one thing a gain report
# must not do.
RGAIN_TERMS = ("short", "long", "all")

# The ``options`` key that turns the per-tag breakdown on, and what an untagged
# remainder row is labelled. The key lives in ``options`` because report_items
# has no column for it and an existing migration is never edited; see the
# module docstring.
BREAK_BY_TAG_OPTION = "break_by_tag"
# The opt-OUT. A second key rather than ``break_by_tag: false`` because the
# breakdown is now the default: "off" is the unusual state and deserves to be
# the one that is written down, while ``break_by_tag`` goes on carrying the
# restrict list of a definition that has one.
NO_TAG_BREAKDOWN_OPTION = "no_tag_breakdown"
UNTAGGED_LABEL = "(untagged)"

RANGE_KINDS = ("fixed", "calendar_year", "preset")
REPORT_KINDS = ("custom", "tax")

_DEF_COLUMNS = (
    "id", "name", "kind", "definition_id", "range_kind", "range_start",
    "range_end", "range_year", "range_preset", "notes", "created_at",
)
_ITEM_COLUMNS = (
    "id", "report_id", "seq", "name", "label", "group_label", "kind", "sign",
    "tag_enabled", "options", "expr", "txf_refnum", "txf_copy", "txf_format",
)


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReportDef:
    """One saved report definition -- the header row of ``report_defs``."""
    id: int
    name: str
    kind: str = "custom"
    definition_id: Optional[str] = None
    range_kind: str = "fixed"
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    range_year: Optional[int] = None
    range_preset: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = ""


@dataclass(frozen=True)
class ReportItem:
    """One line item of a report."""
    id: int
    report_id: int
    seq: int
    name: str
    kind: str
    label: Optional[str] = None
    group_label: Optional[str] = None
    sign: int = 1
    tag_enabled: int = 0
    options: Optional[str] = None
    expr: Optional[str] = None
    txf_refnum: Optional[int] = None
    txf_copy: int = 1
    txf_format: Optional[int] = None

    @property
    def display_label(self) -> str:
        """What a report prints for this item -- ``label`` if the user gave one,
        otherwise the item's (tag-shaped) name."""
        return self.label or self.name

    def options_dict(self) -> dict:
        """``options`` parsed, or ``{}`` -- a corrupt blob reads as empty rather
        than killing an evaluation."""
        if not self.options:
            return {}
        try:
            value = json.loads(self.options)
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def break_by_tag(self) -> Optional[tuple[str, ...]]:
        """The per-tag breakdown setting: ``None`` = off, ``()`` = every tag
        value the matched lines carry, a tuple = only those tag names.

        ON is the DEFAULT, so an item that says nothing reads as ``()``. Only an
        explicit opt-out (``no_tag_breakdown``, what the editor writes) or the
        ``break_by_tag: false`` an older build wrote reads as ``None``.

        A property and not a column: ``report_items`` has none, migrations are
        append-only and this rides ``options`` (see the module docstring). Junk
        in the blob reads as the DEFAULT, the same way ``options_dict`` reads a
        corrupt blob as empty -- an unreadable setting must not kill an
        evaluation."""
        opts = self.options_dict()
        raw = opts.get(BREAK_BY_TAG_OPTION)
        if opts.get(NO_TAG_BREAKDOWN_OPTION) or raw is False:
            return None
        if raw is None or raw is True:
            return ()
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return ()
        names: dict[str, str] = {}
        for entry in raw:
            text = str(entry).strip()
            # Keyed by casefold because ``tags.name`` is COLLATE NOCASE: two
            # spellings of one tag are one tag, and listing both would emit two
            # rows summing the same money onto two TXF copies.
            if text:
                names.setdefault(text.casefold(), text)
        return tuple(names.values())


@dataclass(frozen=True)
class ReportRow:
    """One evaluated line: ``amount`` is signed cents, already multiplied by the
    item's ``sign``. ``raw_amount`` is what was actually summed, before the sign
    flip -- kept because it is the number to audit against the register, while a
    ``COMPUTED`` expression reads the presented ``amount`` instead.

    ``unpriced`` names the securities this row HELD but could not value, because
    no price exists on or before the as-of date. The row still carries the total
    of what could be priced; :attr:`incomplete` says that total is a floor, not
    the answer. ``no_data`` is the weaker signal -- nothing fed the row at all --
    which is what tells "the category did not exist that year" apart from "it
    came to zero".

    ``is_breakdown`` marks a per-tag SUB-ROW of an item that has ``break_by_tag``
    set: ``tag_value`` is the tag it sums, or ``None`` for the untagged
    remainder. Sub-rows share their item's ``item_id`` and ``name`` and come
    BEFORE its total, which is the row everything that predates the breakdown
    (``by_name``, :func:`compare`, the export) keeps seeing. ``txf_copy`` is set
    only on a sub-row, where it is the TXF copy number that sub-row owns."""
    item_id: int
    name: str
    label: str
    group_label: Optional[str]
    kind: str
    sign: int
    amount: int
    raw_amount: int
    line_count: int = 0
    unpriced: tuple[str, ...] = ()
    is_breakdown: bool = False
    tag_value: Optional[str] = None
    txf_copy: Optional[int] = None

    @property
    def incomplete(self) -> bool:
        """True when a price was missing, so ``amount`` understates the truth."""
        return bool(self.unpriced)

    @property
    def no_data(self) -> bool:
        """Nothing was found to sum: zero lines and zero cents."""
        return self.line_count == 0 and self.raw_amount == 0

    @property
    def is_untagged(self) -> bool:
        """This is the untagged remainder of a broken-down item."""
        return self.is_breakdown and self.tag_value is None


@dataclass(frozen=True)
class LineClaim:
    """A register line, and the item names that claimed it.

    ``pair_txn_id`` is set only on a ``transfer_double_counted`` warning: it is
    the OTHER leg of the transfer, so a panel can show the user both rows of the
    pair it is complaining about."""
    txn_id: int
    date: str
    account_id: int
    category_id: Optional[int]
    amount: int
    is_split_line: bool
    item_names: tuple[str, ...] = ()
    pair_txn_id: Optional[int] = None


@dataclass(frozen=True)
class IgnoredTag:
    """An item that is ``tag_enabled`` but whose kind cannot honour a tag.

    "Include this transaction in an end-of-year balance" is not a coherent
    instruction, so ``N``/``!N`` are ignored on every kind but ``SOSC`` -- but a
    user who tagged a transaction and saw no change deserves to be told which
    item ignored it, not left to guess.
    """
    item_id: int
    name: str
    kind: str


@dataclass(frozen=True)
class Coverage:
    """What the evaluated report does and does not account for.

    A PANEL, never a block: every field here is something the user may have
    meant, and the report renders regardless.

    ``multi_claimed``
        Lines claimed by two or more items of THIS report. Allowed and expected
        -- one category can feed several tax lines -- but it is why item totals
        do not sum to a grand total, so it is surfaced rather than blocked.
    ``unclaimed``
        Lines inside the union of the report's category selections that no item
        claimed -- i.e. money the user pointed the report at that fell out of it
        again, which on a tax report is the first thing to read. Reachable only
        through an exclusion tag (``!N`` on every item that selected the
        category), since a selected category otherwise claims its own lines.
    ``transfer_double_counted``
        Both legs of one transfer pulled into the same item by an inclusion tag
        -- a guaranteed double count. One entry per pair, carrying the item name
        and the date, and NEVER silently de-duplicated: the user may have meant
        an external-looking pair, and quietly halving his number is worse.
    ``ignored_tags``
        Items whose kind ignores tags (see :class:`IgnoredTag`).
    """
    multi_claimed: tuple[LineClaim, ...] = ()
    unclaimed: tuple[LineClaim, ...] = ()
    transfer_double_counted: tuple[LineClaim, ...] = ()
    ignored_tags: tuple[IgnoredTag, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.multi_claimed or self.unclaimed
                    or self.transfer_double_counted or self.ignored_tags)


@dataclass(frozen=True)
class ItemDetail:
    """The lines ONE ``SOSC`` item saw, kept only when ``evaluate(detail=True)``.

    ``admitted`` is exactly what the item's total summed, in ledger order.
    ``excluded`` is the lines an exclusion tag pushed out that would OTHERWISE
    have been admitted -- not every line carrying ``!name``, which would sweep in
    the whole ledger, but the ones the item genuinely lost. They are what the
    drill-down strikes through: money the user can see was considered and
    removed, rather than money that silently never appeared.

    This is retained INSIDE the evaluation loop rather than recomputed by a
    second walk, so the drill-down cannot drift from the totals it explains."""
    item_id: int
    admitted: tuple[Line, ...] = ()
    excluded: tuple[Line, ...] = ()
    #: ``(label, cents)`` for a kind whose number is a DIFFERENCE rather than a
    #: sum of lines -- the two valuations behind a NETGAIN, the proceeds and
    #: basis behind a RGAIN. They explain the figure; they are not a partition
    #: of it and nothing sums them (see ``drill_down``).
    parts: tuple = ()


@dataclass(frozen=True)
class Evaluation:
    """The result of :func:`evaluate`: the rows, the range actually used, and
    the coverage warnings.

    ``details`` is empty unless ``detail=True`` was asked for; it is keyed by
    item id and holds only ``SOSC`` items (no other kind has lines to show)."""
    report_id: int
    name: str
    start: str
    end: str
    rows: tuple[ReportRow, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    details: dict = field(default_factory=dict)

    def by_name(self, name: str) -> ReportRow:
        """The named item's TOTAL row. Breakdown sub-rows are skipped on
        purpose: they share their item's name, and every caller that predates
        the breakdown asked for the one number the item reports."""
        for row in self.rows:
            if row.name == name and not row.is_breakdown:
                return row
        raise KeyError(name)

    def amount(self, name: str) -> int:
        """The signed cents of the named item."""
        return self.by_name(name).amount

    def sub_rows(self, name: str) -> tuple[ReportRow, ...]:
        """The named item's per-tag breakdown rows, in report order (the
        untagged remainder last). Empty when the item is not broken down."""
        return tuple(row for row in self.rows
                     if row.name == name and row.is_breakdown)


@dataclass(frozen=True)
class ComparisonColumn:
    """One range of a :func:`compare`, with ITS OWN coverage.

    Coverage is per column and never merged: a category left unclaimed in 2023
    and claimed in 2024 is precisely the discrepancy a comparison exists to
    expose, and a union would hide which year it belongs to."""
    label: str
    start: str
    end: str
    coverage: Coverage = field(default_factory=Coverage)


@dataclass(frozen=True)
class ComparisonRow:
    """One item across every column. ``cells[i]`` is the :class:`ReportRow` from
    column ``i`` -- the whole row, not just its number, so a cell keeps its own
    ``incomplete``/``no_data`` marks."""
    item_id: int
    name: str
    label: str
    group_label: Optional[str]
    kind: str
    sign: int
    cells: tuple[ReportRow, ...] = ()

    @property
    def amounts(self) -> tuple[int, ...]:
        return tuple(c.amount for c in self.cells)


@dataclass(frozen=True)
class ComparisonResult:
    """One definition evaluated over several ranges, rows aligned by ``seq``.

    Alignment is by item IDENTITY, not by position in each evaluation: the same
    definition produces the same items in the same order for every column, and
    keying on the id keeps that true even if a future kind skips a row."""
    report_id: int
    name: str
    columns: tuple[ComparisonColumn, ...] = ()
    rows: tuple[ComparisonRow, ...] = ()

    def by_name(self, name: str) -> ComparisonRow:
        for row in self.rows:
            if row.name == name:
                return row
        raise KeyError(name)

    def amounts(self, name: str) -> tuple[int, ...]:
        """The named item's signed cents, one per column, in column order."""
        return self.by_name(name).amounts

    def cell(self, name: str, column: int) -> ReportRow:
        return self.by_name(name).cells[column]


# --------------------------------------------------------------------------
# Report definition CRUD
# --------------------------------------------------------------------------

def create_report(conn: sqlite3.Connection, name: str, *, kind: str = "custom",
                  definition_id: Optional[str] = None,
                  range_kind: str = "fixed",
                  range_start: Optional[str] = None,
                  range_end: Optional[str] = None,
                  range_year: Optional[int] = None,
                  range_preset: Optional[str] = None,
                  notes: Optional[str] = None) -> int:
    """Create an empty report definition and return its id.

    Names are unique across the ledger (the user picks one from a list), so a
    collision raises :class:`ValueError` rather than an opaque IntegrityError."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a report needs a name")
    if kind not in REPORT_KINDS:
        raise ValueError(f"report kind must be one of {REPORT_KINDS}")
    _validate_range(range_kind, range_start, range_end, range_year, range_preset)
    try:
        cur = conn.execute(
            "INSERT INTO report_defs (name, kind, definition_id, range_kind, "
            "range_start, range_end, range_year, range_preset, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))",
            (name, kind, definition_id, range_kind, range_start, range_end,
             range_year, range_preset, notes))
    except _IntegrityError as exc:
        raise ValueError(f"a report named {name!r} already exists") from exc
    conn.commit()
    return int(cur.lastrowid)


def get_report(conn: sqlite3.Connection, report_id: int) -> ReportDef:
    row = conn.execute(
        f"SELECT {', '.join(_DEF_COLUMNS)} FROM report_defs WHERE id = ?",
        (report_id,)).fetchone()
    if row is None:
        raise KeyError(f"no report {report_id}")
    return _def_from_row(row)


def find_report(conn: sqlite3.Connection, name: str) -> Optional[ReportDef]:
    row = conn.execute(
        f"SELECT {', '.join(_DEF_COLUMNS)} FROM report_defs WHERE name = ?",
        (name,)).fetchone()
    return None if row is None else _def_from_row(row)


def list_reports(conn: sqlite3.Connection) -> list[ReportDef]:
    return [_def_from_row(r) for r in conn.execute(
        f"SELECT {', '.join(_DEF_COLUMNS)} FROM report_defs "
        "ORDER BY name COLLATE NOCASE").fetchall()]


def update_report(conn: sqlite3.Connection, report_id: int, **fields) -> None:
    """Update named columns of a report definition. ``id`` and ``created_at``
    are not updatable; an unknown column raises :class:`ValueError`."""
    current = get_report(conn, report_id)
    allowed = set(_DEF_COLUMNS) - {"id", "created_at"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown report field(s): {sorted(bad)}")
    if not fields:
        return
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise ValueError("a report needs a name")
    if "kind" in fields and fields["kind"] not in REPORT_KINDS:
        raise ValueError(f"report kind must be one of {REPORT_KINDS}")
    merged = {k: getattr(current, k) for k in
              ("range_kind", "range_start", "range_end", "range_year", "range_preset")}
    merged.update({k: v for k, v in fields.items() if k in merged})
    _validate_range(**merged)
    sets = ", ".join(f"{k} = ?" for k in fields)
    try:
        conn.execute(f"UPDATE report_defs SET {sets} WHERE id = ?",
                     (*fields.values(), report_id))
    except _IntegrityError as exc:
        raise ValueError(f"a report named {fields.get('name')!r} already exists") from exc
    conn.commit()


def delete_report(conn: sqlite3.Connection, report_id: int) -> None:
    """Delete a report and, by cascade, its items and their selections."""
    conn.execute("DELETE FROM report_defs WHERE id = ?", (report_id,))
    conn.commit()


# --------------------------------------------------------------------------
# Item CRUD
# --------------------------------------------------------------------------

def add_item(conn: sqlite3.Connection, report_id: int, name: str, kind: str, *,
             label: Optional[str] = None, group_label: Optional[str] = None,
             seq: Optional[int] = None, sign: int = 1, tag_enabled: int = 0,
             options: Optional[str | dict] = None, expr: Optional[str] = None,
             txf_refnum: Optional[int] = None, txf_copy: int = 1,
             txf_format: Optional[int] = None) -> int:
    """Append a line item to a report and return its id.

    ``seq`` defaults to the end of the list. Item names are unique WITHIN a
    report; a ``tag_enabled`` name must additionally be usable as a TAG name, so
    it goes through ``ledger.validate_report_item_tag_name`` -- which owns tag
    naming and checks uniqueness LEDGER-wide, because two reports whose items
    share a tag-enabled name would fight over the same tag."""
    name = (name or "").strip()
    if not name:
        raise ValueError("an item needs a name")
    if tag_enabled:
        name = ledger.validate_report_item_tag_name(conn, name)
    if kind not in ALL_KINDS:
        raise ValueError(f"item kind must be one of {ALL_KINDS}")
    if sign not in (1, -1):
        raise ValueError("sign must be +1 or -1")
    get_report(conn, report_id)               # KeyError if the report is gone
    if kind == "COMPUTED" and (expr or "").strip():
        validate_expr(expr)
    # An empty expression is allowed HERE and refused at evaluation: the editor
    # adds the row before the formula is typed, and a half-built report should
    # not be unsaveable.
    _check_name_cycle(conn, report_id, name=name, kind=kind, expr=expr)
    if seq is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM report_items "
            "WHERE report_id = ?", (report_id,)).fetchone()
        seq = int(row["n"])
    if isinstance(options, dict):
        options = json.dumps(options, sort_keys=True)
    try:
        cur = conn.execute(
            "INSERT INTO report_items (report_id, seq, name, label, group_label, "
            "kind, sign, tag_enabled, options, expr, txf_refnum, txf_copy, "
            "txf_format) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (report_id, seq, name, label, group_label, kind, sign,
             1 if tag_enabled else 0, options, expr, txf_refnum, txf_copy,
             txf_format))
    except _IntegrityError as exc:
        raise ValueError(
            f"report {report_id} already has an item named {name!r}") from exc
    conn.commit()
    _resync_report_refs(conn, report_id)
    return int(cur.lastrowid)


def get_item(conn: sqlite3.Connection, item_id: int) -> ReportItem:
    row = conn.execute(
        f"SELECT {', '.join(_ITEM_COLUMNS)} FROM report_items WHERE id = ?",
        (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"no report item {item_id}")
    return _item_from_row(row)


def list_items(conn: sqlite3.Connection, report_id: int) -> list[ReportItem]:
    return [_item_from_row(r) for r in conn.execute(
        f"SELECT {', '.join(_ITEM_COLUMNS)} FROM report_items "
        "WHERE report_id = ? ORDER BY seq, id", (report_id,)).fetchall()]


def update_item(conn: sqlite3.Connection, item_id: int, **fields) -> None:
    """Update named columns of an item. ``id`` and ``report_id`` are fixed.

    Turning ``tag_enabled`` ON re-validates the name as a tag name even when the
    name itself is unchanged -- otherwise an item created as plain text could be
    promoted to a tag later and smuggle in a comma."""
    current = get_item(conn, item_id)
    allowed = set(_ITEM_COLUMNS) - {"id", "report_id"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown item field(s): {sorted(bad)}")
    if not fields:
        return
    if "kind" in fields and fields["kind"] not in ALL_KINDS:
        raise ValueError(f"item kind must be one of {ALL_KINDS}")
    if "sign" in fields and fields["sign"] not in (1, -1):
        raise ValueError("sign must be +1 or -1")
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise ValueError("an item needs a name")
    if isinstance(fields.get("options"), dict):
        fields["options"] = json.dumps(fields["options"], sort_keys=True)
    if "tag_enabled" in fields:
        fields["tag_enabled"] = 1 if fields["tag_enabled"] else 0
    if ("name" in fields or "tag_enabled" in fields) and \
            fields.get("tag_enabled", current.tag_enabled):
        clean = ledger.validate_report_item_tag_name(
            conn, fields.get("name", current.name), item_id=item_id)
        if "name" in fields:
            fields["name"] = clean
    new_kind = fields.get("kind", current.kind)
    new_expr = fields.get("expr", current.expr)
    touches_graph = bool({"name", "kind", "expr"} & set(fields))
    if touches_graph:
        if new_kind == "COMPUTED" and (new_expr or "").strip():
            validate_expr(new_expr)
        _check_name_cycle(conn, current.report_id, item_id=item_id,
                          name=fields.get("name", current.name),
                          kind=new_kind, expr=new_expr)
    sets = ", ".join(f"{k} = ?" for k in fields)
    try:
        conn.execute(f"UPDATE report_items SET {sets} WHERE id = ?",
                     (*fields.values(), item_id))
    except _IntegrityError as exc:
        raise ValueError(
            f"that report already has an item named {fields.get('name')!r}") from exc
    conn.commit()
    if touches_graph:
        # A RENAME re-points every expression that used the old name, which is
        # why the whole report is re-synced and not just this row.
        _resync_report_refs(conn, current.report_id)


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    """Delete an item and, by cascade, its category/account/security rows.

    The edges pointing AT it go with the cascade; the edges pointing FROM other
    items' expressions at its NAME are re-synced away, so what remains is an
    expression naming a line that no longer exists -- refused at evaluation."""
    try:
        report_id = get_item(conn, item_id).report_id
    except KeyError:
        report_id = None
    conn.execute("DELETE FROM report_items WHERE id = ?", (item_id,))
    conn.commit()
    if report_id is not None:
        _resync_report_refs(conn, report_id)


def reorder_items(conn: sqlite3.Connection, report_id: int,
                  item_ids: Sequence[int]) -> None:
    """Renumber ``seq`` to the given order. Ids not listed keep their relative
    order after the listed ones."""
    listed = [int(i) for i in item_ids]
    known = [it.id for it in list_items(conn, report_id)]
    unknown = set(listed) - set(known)
    if unknown:
        raise ValueError(f"item(s) not in report {report_id}: {sorted(unknown)}")
    order = listed + [i for i in known if i not in set(listed)]
    for seq, item_id in enumerate(order):
        conn.execute("UPDATE report_items SET seq = ? WHERE id = ?", (seq, item_id))
    conn.commit()


# --------------------------------------------------------------------------
# Selections
# --------------------------------------------------------------------------

def set_item_categories(conn: sqlite3.Connection, item_id: int,
                        selections: Iterable) -> None:
    """Replace an item's category selection.

    ``selections`` is an iterable of ``category_id`` (subtree off) or of
    ``(category_id, include_subtree)`` pairs, or a ``{id: include_subtree}``
    mapping. Ids are stored VERBATIM -- including a parent the picker left
    partially checked, which means "this parent's own postings, plus the
    children still ticked" (SRD 5.9c) and must not be filtered out."""
    rows = _normalize_category_selections(selections)
    get_item(conn, item_id)
    conn.execute("DELETE FROM report_item_categories WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO report_item_categories (item_id, category_id, include_subtree) "
        "VALUES (?,?,?)", [(item_id, cid, sub) for cid, sub in rows])
    conn.commit()


def item_categories(conn: sqlite3.Connection, item_id: int) -> list[tuple[int, int]]:
    """``[(category_id, include_subtree), ...]`` as stored, in id order."""
    return [(int(r["category_id"]), int(r["include_subtree"])) for r in conn.execute(
        "SELECT category_id, include_subtree FROM report_item_categories "
        "WHERE item_id = ? ORDER BY category_id", (item_id,)).fetchall()]


def set_item_accounts(conn: sqlite3.Connection, item_id: int,
                      account_ids: Iterable[int]) -> None:
    """Replace an item's account selection (the balance kinds)."""
    ids = sorted({int(a) for a in account_ids})
    get_item(conn, item_id)
    conn.execute("DELETE FROM report_item_accounts WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO report_item_accounts (item_id, account_id) VALUES (?,?)",
        [(item_id, a) for a in ids])
    conn.commit()


def item_accounts(conn: sqlite3.Connection, item_id: int) -> list[int]:
    """The account ids still selected. A deleted account cascades out of this
    list, which is why a report survives one."""
    return [int(r["account_id"]) for r in conn.execute(
        "SELECT account_id FROM report_item_accounts WHERE item_id = ? "
        "ORDER BY account_id", (item_id,)).fetchall()]


def set_item_securities(conn: sqlite3.Connection, item_id: int,
                        symbols: Iterable[str]) -> None:
    """Replace a ``HOLDVAL``/``RGAIN`` item's security selection.

    Symbols are stored as given (the ``securities`` table owns their canonical
    case) and an EMPTY selection is not "nothing": it means every symbol the
    selected accounts held, which is what a portfolio-level line wants."""
    wanted = []
    seen = set()
    for sym in symbols:
        sym = (sym or "").strip()
        if sym and sym.upper() not in seen:
            seen.add(sym.upper())
            wanted.append(sym)
    get_item(conn, item_id)
    conn.execute("DELETE FROM report_item_securities WHERE item_id = ?", (item_id,))
    try:
        conn.executemany(
            "INSERT INTO report_item_securities (item_id, symbol) VALUES (?,?)",
            [(item_id, s) for s in sorted(wanted)])
    except _IntegrityError as exc:
        raise ValueError(
            f"no such security in {sorted(wanted)}: a report can only select a "
            "symbol the ledger knows") from exc
    conn.commit()


def item_securities(conn: sqlite3.Connection, item_id: int) -> list[str]:
    """The symbols still selected, in symbol order. Empty means "all held"."""
    return [str(r["symbol"]) for r in conn.execute(
        "SELECT symbol FROM report_item_securities WHERE item_id = ? "
        "ORDER BY symbol", (item_id,)).fetchall()]


def items_using_category(conn: sqlite3.Connection, category_id: int) -> list[tuple[str, str]]:
    """``[(report_name, item_name), ...]`` -- what the Category Manager's delete
    confirmation needs to say before a category takes report items with it."""
    return [(r["report_name"], r["item_name"]) for r in conn.execute(
        "SELECT d.name AS report_name, i.name AS item_name "
        "FROM report_item_categories c "
        "JOIN report_items i ON i.id = c.item_id "
        "JOIN report_defs d ON d.id = i.report_id "
        "WHERE c.category_id = ? "
        "ORDER BY d.name COLLATE NOCASE, i.seq", (category_id,)).fetchall()]


# --------------------------------------------------------------------------
# COMPUTED expressions and their dependency edges
# --------------------------------------------------------------------------

# A brace name is anything between braces except a brace: item names are the
# user's own words and may contain spaces, colons and punctuation ("TI:tithe
# due"), so the delimiters do the work and the contents are taken verbatim
# (stripped). Matching is EXACT, like report_defs._unresolved_expr_refs -- two
# items differing only in case are two items, and guessing between them is worse
# than saying the name is unknown.
_BRACE_NAME = re.compile(r"\{([^{}]*)\}")

# The whole grammar, in one tokenizer: a brace name, a number, or one of six
# operator characters. Anything else is a syntax error, which is the point --
# an expression is DATA, and there is no production here that could reach a
# Python name, an attribute or a call.
_EXPR_TOKEN = re.compile(r"""
      \{(?P<name>[^{}]*)\}
    | (?P<num>\d+(?:\.\d*)?|\.\d+)
    | (?P<op>[-+*/()])
    | (?P<ws>\s+)
""", re.VERBOSE)

# Parenthesis nesting is bounded so a pathological definition file cannot walk
# the parser into a RecursionError. Nobody hand-writes 64 nested parentheses.
_MAX_EXPR_DEPTH = 64


def expr_names(expr: Optional[str]) -> list[str]:
    """The brace names an expression references, in order, without duplicates."""
    out: list[str] = []
    for raw in _BRACE_NAME.findall(expr or ""):
        name = raw.strip()
        if name and name not in out:
            out.append(name)
    return out


def validate_expr(expr: Optional[str]) -> None:
    """Raise :class:`ValueError` if ``expr`` is not a legal COMPUTED expression.

    Names are NOT resolved here -- this is the grammar check, and it stands in
    every referent as ``1`` so that a syntactically fine ``{a} / ({b} - {c})``
    is not failed for a division by zero it will not actually perform."""
    text = (expr or "").strip()
    if not text:
        raise ValueError("a COMPUTED item needs an expression")
    _eval_expr(text, lambda _name: Decimal(1), strict_div=False)


def _eval_expr(expr: str, resolve: Callable[[str], Decimal], *,
               strict_div: bool = True) -> Decimal:
    """Evaluate one restricted expression. ``resolve`` turns a brace name into a
    number of cents (and may raise :class:`ValueError` for an unknown name)."""
    return _ExprParser(_tokenize(expr), resolve, strict_div=strict_div).parse()


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        match = _EXPR_TOKEN.match(expr, pos)
        if match is None:
            raise ValueError(
                f"unexpected character {expr[pos]!r} in expression "
                "(only names in braces, numbers, + - * / and parentheses)")
        pos = match.end()
        if match.lastgroup == "ws":
            continue
        if match.lastgroup == "name":
            name = match.group("name").strip()
            if not name:
                raise ValueError("empty {} in expression")
            tokens.append(("name", name))
        else:
            tokens.append((match.lastgroup, match.group(match.lastgroup)))
    if not tokens:
        raise ValueError("a COMPUTED item needs an expression")
    return tokens


class _ExprParser:
    """Recursive descent over the token list.

    Arithmetic is ``Decimal`` on integer cents -- never a float, which would
    make the eighth digit of a tithe a lottery. Rounding happens at every
    DIVISION rather than only at the end, because a division is where the user's
    own pencil would round: he checks ``151,586.5 -> 151,587``, not a trailing
    remainder carried through three more terms.
    """

    def __init__(self, tokens: Sequence[tuple[str, str]],
                 resolve: Callable[[str], Decimal], *,
                 strict_div: bool = True) -> None:
        self._toks = list(tokens)
        self._pos = 0
        self._resolve = resolve
        self._strict_div = strict_div

    def parse(self) -> Decimal:
        value = self._expr(0)
        if self._pos != len(self._toks):
            raise ValueError(
                f"unexpected {self._toks[self._pos][1]!r} in expression")
        return value

    def _peek(self) -> Optional[tuple[str, str]]:
        return self._toks[self._pos] if self._pos < len(self._toks) else None

    def _expr(self, depth: int) -> Decimal:
        value = self._term(depth)
        while True:
            tok = self._peek()
            if tok is None or tok[0] != "op" or tok[1] not in ("+", "-"):
                return value
            self._pos += 1
            rhs = self._term(depth)
            value = value + rhs if tok[1] == "+" else value - rhs

    def _term(self, depth: int) -> Decimal:
        value = self._factor(depth)
        while True:
            tok = self._peek()
            if tok is None or tok[0] != "op" or tok[1] not in ("*", "/"):
                return value
            self._pos += 1
            rhs = self._factor(depth)
            if tok[1] == "*":
                value = value * rhs
            elif rhs == 0:
                if self._strict_div:
                    raise ValueError("division by zero")
                value = Decimal(0)
            else:
                value = (value / rhs).quantize(Decimal(1),
                                               rounding=ROUND_HALF_UP)

    def _factor(self, depth: int) -> Decimal:
        tok = self._peek()
        if tok is not None and tok[0] == "op" and tok[1] in ("+", "-"):
            self._pos += 1
            value = self._factor(depth)
            return -value if tok[1] == "-" else value
        return self._primary(depth)

    def _primary(self, depth: int) -> Decimal:
        tok = self._peek()
        if tok is None:
            raise ValueError("expression ends early")
        kind, text = tok
        self._pos += 1
        if kind == "name":
            return Decimal(self._resolve(text))
        if kind == "num":
            return Decimal(text)
        if text == "(":
            if depth >= _MAX_EXPR_DEPTH:
                raise ValueError("expression nests too deeply")
            value = self._expr(depth + 1)
            nxt = self._peek()
            if nxt is None or nxt[1] != ")":
                raise ValueError("unbalanced parentheses in expression")
            self._pos += 1
            return value
        raise ValueError(f"unexpected {text!r} in expression")


def item_refs(conn: sqlite3.Connection, item_id: int) -> list[int]:
    """The item ids this item's expression depends on, in id order."""
    return [int(r["ref_item_id"]) for r in conn.execute(
        "SELECT ref_item_id FROM report_item_refs WHERE item_id = ? "
        "ORDER BY ref_item_id", (item_id,)).fetchall()]


def report_refs(conn: sqlite3.Connection, report_id: int) -> dict[int, list[int]]:
    """The stored dependency graph of one report: ``{item_id: [ref_id, ...]}``,
    every item present, expression-less ones mapping to an empty list."""
    graph: dict[int, list[int]] = {}
    for item in list_items(conn, report_id):
        graph[item.id] = []
    for row in conn.execute(
            "SELECT r.item_id AS a, r.ref_item_id AS b FROM report_item_refs r "
            "JOIN report_items i ON i.id = r.item_id "
            "WHERE i.report_id = ? ORDER BY r.item_id, r.ref_item_id",
            (report_id,)).fetchall():
        graph.setdefault(int(row["a"]), []).append(int(row["b"]))
    return graph


def _resync_report_refs(conn: sqlite3.Connection, report_id: int) -> None:
    """Rewrite one report's dependency edges from its expressions.

    The WHOLE report is re-synced after any item write, not just the item that
    changed, because that is what makes a forward reference work: a definition
    file that lists the total before its parts stores no edge for ``{parts}``
    the first time, and picks it up when the referent is created. A name that
    still resolves to nothing stores no edge and is refused at evaluation --
    silently zeroing it would turn a broken formula into a plausible number."""
    items = list_items(conn, report_id)
    by_name = {item.name: item.id for item in items}
    ids = [item.id for item in items]
    if ids:
        marks = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM report_item_refs WHERE item_id IN ({marks})",
                     tuple(ids))
    edges: list[tuple[int, int]] = []
    for item in items:
        if item.kind != "COMPUTED" or not item.expr:
            continue
        for name in expr_names(item.expr):
            ref = by_name.get(name)
            if ref is not None:
                edges.append((item.id, ref))
    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO report_item_refs (item_id, ref_item_id) "
            "VALUES (?,?)", edges)
    conn.commit()


def _find_cycle(graph: dict) -> Optional[list]:
    """A cycle in ``graph`` as the list of nodes around it (first node repeated
    at the end), or None.

    ITERATIVE on purpose. The thing being defended against is unbounded
    recursion on a corrupt graph, and a recursive detector would hit the same
    wall it is meant to guard."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    for start in graph:
        if color[start] != WHITE:
            continue
        color[start] = GREY
        path = [start]
        stack = [(start, iter(graph.get(start, ())))]
        while stack:
            node, edges = stack[-1]
            advanced = False
            for nxt in edges:
                if nxt not in color:
                    continue                      # edge to something not here
                if color[nxt] == GREY:
                    return path[path.index(nxt):] + [nxt]
                if color[nxt] == WHITE:
                    color[nxt] = GREY
                    path.append(nxt)
                    stack.append((nxt, iter(graph.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
                path.pop()
    return None


def _check_name_cycle(conn: sqlite3.Connection, report_id: int, *,
                      item_id: Optional[int] = None,
                      name: Optional[str] = None,
                      kind: Optional[str] = None,
                      expr: Optional[str] = None) -> None:
    """Refuse a save that would make the report's expressions circular.

    Checked on the PROSPECTIVE graph, before the write, and by NAME -- because
    the item may not exist yet (``add_item``), so it has no id to graph, and
    because the message has to name the loop in the user's own words."""
    graph: dict[str, list[str]] = {}
    for item in list_items(conn, report_id):
        if item_id is not None and item.id == item_id:
            continue
        graph[item.name] = (expr_names(item.expr)
                            if item.kind == "COMPUTED" else [])
    if name is not None:
        graph[name] = expr_names(expr) if kind == "COMPUTED" else []
    loop = _find_cycle(graph)
    if loop is not None:
        raise ValueError("expression cycle: " + " -> ".join(loop))


def _check_stored_cycle(conn: sqlite3.Connection, report_id: int,
                        items: Sequence[ReportItem]) -> None:
    """Re-check for cycles at evaluation time, over the UNION of the stored
    edges and the edges the expressions imply.

    Not redundant with the save-time check: ``report_item_refs`` and
    ``report_items.expr`` are ordinary rows, and one hand-written ``UPDATE`` --
    or a definition restored from a file written by an older build -- can leave
    a loop behind. Evaluating it would recurse until the interpreter dies, so it
    raises here instead, naming the loop."""
    by_name = {item.name: item.id for item in items}
    names = {item.id: item.name for item in items}
    graph: dict[int, list[int]] = {item.id: [] for item in items}
    for item_id, refs in report_refs(conn, report_id).items():
        if item_id in graph:
            graph[item_id] = [r for r in refs if r in graph]
    for item in items:
        if item.kind != "COMPUTED" or not item.expr:
            continue
        for ref_name in expr_names(item.expr):
            ref = by_name.get(ref_name)
            if ref is not None and ref not in graph[item.id]:
                graph[item.id].append(ref)
    loop = _find_cycle(graph)
    if loop is not None:
        raise ValueError("expression cycle: "
                         + " -> ".join(names.get(i, str(i)) for i in loop))


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def resolve_range(conn: sqlite3.Connection, report: ReportDef | int, *,
                  today: Optional[_dt.date] = None) -> tuple[str, str]:
    """The inclusive ISO ``(start, end)`` the report's stored range means."""
    rd = report if isinstance(report, ReportDef) else get_report(conn, report)
    if rd.range_kind == "fixed":
        if not (rd.range_start and rd.range_end):
            raise ValueError(f"report {rd.name!r} has no fixed range stored")
        return rd.range_start, rd.range_end
    if rd.range_kind == "calendar_year":
        if not rd.range_year:
            raise ValueError(f"report {rd.name!r} has no range_year stored")
        return f"{int(rd.range_year):04d}-01-01", f"{int(rd.range_year):04d}-12-31"
    if rd.range_kind == "preset":
        if not rd.range_preset:
            raise ValueError(f"report {rd.name!r} has no range_preset stored")
        from mammon.reports.spending import preset_range   # lazy: avoids a cycle
        return preset_range(rd.range_preset, today or _dt.date.today())
    raise ValueError(f"unknown range_kind {rd.range_kind!r}")


def evaluate(conn: sqlite3.Connection, report_id: int,
             start: Optional[str] = None, end: Optional[str] = None,
             *, today: Optional[_dt.date] = None,
             detail: bool = False) -> Evaluation:
    """Evaluate every item of ``report_id`` over ``[start, end]``.

    ``start``/``end`` override the report's stored range (that is how
    :func:`compare` re-points one definition at several years); omit both and
    the stored range is resolved through :func:`resolve_range`.

    ``detail=True`` additionally keeps the lines each SOSC item admitted and the
    ones an exclusion tag pushed out (:class:`ItemDetail`), which is what
    :func:`drill_down` shapes into a tree. It is off by default so the ordinary
    path allocates nothing extra, and it is a FLAG on this function rather than a
    second walk of the ledger because a drill-down that disagreed with the total
    it sits under would be worse than no drill-down at all."""
    rd = get_report(conn, report_id)
    if start is None or end is None:
        stored_start, stored_end = resolve_range(conn, rd, today=today)
        start = start or stored_start
        end = end or stored_end
    validate_date(start)
    validate_date(end)
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    items = list_items(conn, report_id)
    # Re-checked here and not only at save: see _check_stored_cycle.
    _check_stored_cycle(conn, report_id, items)

    lines: Optional[list[Line]] = None          # built once, only if an SOSC asks
    tag_sets: list[set[str]] = []               # line index -> effective tag set
    claims: dict[int, list[str]] = {}           # line index -> item names
    selected_any: set[int] = set()
    row_by_id: dict[int, ReportRow] = {}        # filled out of seq order
    sub_by_id: dict[int, list[ReportRow]] = {}  # item id -> per-tag breakdown
    computed: list[ReportItem] = []             # deferred to the topological pass
    doubles: list[LineClaim] = []
    ignored: list[IgnoredTag] = []
    details: dict[int, ItemDetail] = {}         # item id -> lines, when detail=True
    # NOTE: there is deliberately no report-wide set of "machinery" tag names
    # here. One existed, holding the name of every tag-enabled item, and the
    # breakdown refused to bucket by any of them; in the rental shape those
    # names ARE the property tags, so every sub-row came out 0.00. Only the
    # item's OWN inclusion tag is suppressed now, per item -- see the docstring.

    for item in items:
        if item.kind == "SOSC":
            if lines is None:
                # transfers="all" because a transfer leg is invisible to the
                # default filter, and an inclusion tag has to be able to reach
                # one. It cannot disturb the category path or the coverage
                # arithmetic: a transfer line carries category_id=None, so it is
                # never selected by category and never counts as "unclaimed"
                # (None is in no selection set).
                lines = signed_lines(conn, start, end, _all_account_ids(conn),
                                     transfers="all")
                tag_sets = [effective_tags(ln) for ln in lines]
            wanted = _selected_category_ids(conn, item.id)
            selected_any |= wanted
            # Transfer counterparties this item selected (rule 4 in ``_admit``).
            # SOSC is the only kind that reads ``report_item_accounts`` this way:
            # for the balance kinds the same rows mean "the accounts to value",
            # which is why this is a separate accessor with its own name rather
            # than a second caller of ``item_accounts``.
            transfers = _selected_transfer_ids(conn, item.id)
            # ``tag_enabled`` gates INCLUSION only. Pulling a line in by name is
            # a standing promise about a whole vocabulary -- "any line tagged
            # this joins this item" -- and an item has to opt into it. Pushing
            # ONE line out is not the same promise in the other direction: it
            # names a single posting the user is looking at, it cannot drag
            # anything in, and there is nothing to opt into. Gating both on the
            # same flag meant the report's right-click wrote a tag that 67 of 68
            # items then ignored -- a silent no-op that dirtied the tag table.
            folded = item.name.casefold() if item.tag_enabled else None
            negated = ("!" + item.name).casefold()
            total = 0
            count = 0
            tagged_transfers: list[int] = []
            # The breakdown buckets a line only AFTER the three rules below have
            # admitted it, so no bucketing can move ``total``.
            breakdown = item.break_by_tag
            restrict = ({n.casefold(): n for n in breakdown} or None
                        if breakdown is not None else None)
            buckets: dict[str, list] = {}       # casefold -> [display, cents, n]
            remainder = [0, 0]                  # cents, line count
            admitted: list[Line] = []
            excluded: list[Line] = []
            for idx, line in enumerate(lines):
                tags = tag_sets[idx]
                verdict = _admit(line, tags, wanted, folded, negated,
                                 transfers)
                if verdict is SKIP:
                    continue
                if verdict is EXCLUDED:
                    # Eligible, then pushed out by rule 1. It moves no money and
                    # makes no claim -- it is kept only so the drill-down can show
                    # the user what his !tag removed.
                    if detail:
                        excluded.append(line)
                    continue
                included = folded is not None and folded in tags
                total += int(line.amount)
                count += 1
                claims.setdefault(idx, []).append(item.name)
                if detail:
                    admitted.append(line)
                if line.transfer_account_id is not None and (
                        included
                        or line.transfer_account_id in transfers):
                    # Both ways a transfer can be pulled in feed the double-count
                    # check: selecting the two accounts of one transfer is the
                    # same mistake as tagging both its legs.
                    tagged_transfers.append(idx)
                if breakdown is not None:
                    hits = _breakdown_tags(line, folded, restrict)
                    for fold, display in hits:
                        slot = buckets.setdefault(fold, [display, 0, 0])
                        slot[1] += int(line.amount)
                        slot[2] += 1
                    if not hits:                # no bucket claims it
                        remainder[0] += int(line.amount)
                        remainder[1] += 1
            row_by_id[item.id] = _row(item, total, count)
            if detail:
                details[item.id] = ItemDetail(item_id=item.id,
                                              admitted=tuple(admitted),
                                              excluded=tuple(excluded))
            if breakdown is not None:
                subs = _breakdown_rows(item, breakdown, buckets, remainder)
                if subs:            # discovery found no tag: no sub-rows at all
                    sub_by_id[item.id] = subs
            doubles.extend(
                _transfer_double_counts(conn, lines, tagged_transfers, item.name))
        elif item.kind in ("EDAB", "SDAB"):
            as_of = end if item.kind == "EDAB" else _day_before(start)
            accounts = item_accounts(conn, item.id)
            total = sum(_display_balance(conn, aid, as_of) for aid in accounts)
            row_by_id[item.id] = _row(item, total, len(accounts),
                                      _unpriced_symbols(conn, accounts, as_of))
        elif item.kind == "HOLDVAL":
            accounts = item_accounts(conn, item.id)
            total, unpriced, count = _holdings_value(
                conn, accounts, item_securities(conn, item.id), end)
            row_by_id[item.id] = _row(item, total, count, unpriced)
        elif item.kind == "RGAIN":
            total, count, proceeds, basis = _realized_gain(
                conn, item, start, end)
            row_by_id[item.id] = _row(item, total, count)
            if detail:
                # gain = proceeds - basis, which is the whole of what a realized
                # gain IS; showing only the difference leaves a user unable to
                # tell a small gain on a large sale from a large gain on a small
                # one, and Schedule D wants both footings anyway.
                details[item.id] = ItemDetail(item_id=item.id, parts=(
                    ("Sale proceeds", proceeds),
                    ("Less cost basis", -basis)))
        elif item.kind == "NETGAIN":
            accounts = item_accounts(conn, item.id)
            opening = _day_before(start)
            closing_value = sum(_display_balance(conn, aid, end)
                                for aid in accounts)
            opening_value = sum(_display_balance(conn, aid, opening)
                                for aid in accounts)
            flows = _external_flow_total(conn, accounts, start, end)
            if detail:
                # A gain is a DIFFERENCE, so the number alone is unreadable: it
                # cannot say whether a flat year held a fortune or nothing. The
                # three terms it is built from are what make it checkable, and
                # the flows term is included because without it the two
                # valuations do not reconcile to the gain and the gap looks like
                # an error rather than the deposit it is.
                details[item.id] = ItemDetail(item_id=item.id, parts=(
                    (f"Value on {opening}", opening_value),
                    (f"Value on {end}", closing_value),
                    ("Less money paid in", -flows)))
            total = closing_value
            total -= opening_value
            # What the account GAINED is what it is worth now less what it was
            # worth then, less every dollar the outside world put in (and plus
            # every dollar taken out). A deposit is not a gain.
            total -= flows
            unpriced = tuple(sorted(
                set(_unpriced_symbols(conn, accounts, end))
                | set(_unpriced_symbols(conn, accounts, opening))))
            row_by_id[item.id] = _row(item, total, len(accounts), unpriced)
        elif item.kind == "COMPUTED":
            computed.append(item)               # needs its referents first
        else:
            raise ValueError(f"item {item.name!r}: unknown kind {item.kind!r}")
        if item.tag_enabled and item.kind != "SOSC":
            # Flagged, not honoured, and not silently dropped either.
            ignored.append(IgnoredTag(item_id=item.id, name=item.name,
                                      kind=item.kind))

    _evaluate_computed(computed, items, row_by_id, sub_by_id)

    # Sub-rows first, the item's own total LAST: everything written before the
    # breakdown existed (``by_name``, :func:`compare`, which keeps the last row
    # it sees for an item) must go on reading the total.
    rows: list[ReportRow] = []
    for it in items:
        rows.extend(sub_by_id.get(it.id, ()))
        rows.append(row_by_id[it.id])

    return Evaluation(report_id=rd.id, name=rd.name, start=start, end=end,
                      rows=tuple(rows),
                      coverage=_coverage(lines or [], claims, selected_any,
                                         doubles, ignored),
                      details=details)


def _evaluate_computed(computed: Sequence[ReportItem],
                       items: Sequence[ReportItem],
                       row_by_id: dict[int, ReportRow],
                       sub_by_id: dict[int, list[ReportRow]]) -> None:
    """Resolve the COMPUTED items in dependency order, in place.

    Kahn by repeated pass rather than a precomputed topological sort: the pass
    loop is short (a report has tens of items, not thousands) and it needs no
    graph of its own -- an item is ready exactly when every name it mentions is
    already in ``row_by_id``. A pass that places nothing means the remainder
    depends on itself, which :func:`_check_stored_cycle` should already have
    caught; the error here is the backstop that keeps a miss from becoming an
    infinite loop."""
    if not computed:
        return
    by_name = {item.name: item for item in items}
    pending = list(computed)
    while pending:
        progressed = False
        still: list[ReportItem] = []
        for item in pending:
            if not (item.expr or "").strip():
                raise ValueError(
                    f"item {item.name!r}: a COMPUTED item needs an expression")
            deps = []
            for name in expr_names(item.expr):
                ref = by_name.get(name)
                if ref is None:
                    raise ValueError(
                        f"item {item.name!r}: expression references {{{name}}}, "
                        "which this report has no line named")
                deps.append(ref)
            if any(d.id not in row_by_id for d in deps):
                still.append(item)
                continue
            # A brace name reads the referent's PRESENTED amount -- after its
            # sign -- so what the user adds up on screen is what the formula
            # adds up.
            value = _eval_expr(
                item.expr,
                lambda name: Decimal(row_by_id[by_name[name].id].amount))
            cents = int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
            unpriced = sorted({s for d in deps
                               for s in row_by_id[d.id].unpriced})
            # Incompleteness is contagious: a total built on a row that could
            # not be priced is itself understated, and must say so.
            row_by_id[item.id] = _row(item, cents, len(deps), tuple(unpriced))
            # The opt-out has to be honoured here too: a COMPUTED item builds its
            # sub-rows out of its REFERENTS' sub-rows, so "do not subtotal this
            # total by tag" has to be asked of the total itself.
            if item.break_by_tag is not None:
                subs = _computed_breakdown(item, deps, by_name, sub_by_id)
                if subs:
                    sub_by_id[item.id] = subs
            progressed = True
        if not progressed:
            raise ValueError(
                "expression cycle: "
                + " -> ".join(sorted(item.name for item in still)))
        pending = still


# --------------------------------------------------------------------------
# Drill-down: the report as a tree, item -> tag -> category -> transaction
# --------------------------------------------------------------------------
#
# The window renders this instead of a flat table, so every figure on a tax form
# can be opened until the transactions that made it are on screen. Three rules
# hold the shape honest:
#
# * **Nothing here sums a child into a parent.** Each node reports what its OWN
#   lines came to, and an item node reports :func:`evaluate`'s row verbatim. That
#   is not an optimization -- it is the invariant. A line carrying two of the
#   item's tags is deliberately counted under each (see the module docstring), so
#   tag nodes can legitimately add up to more than the item above them, and a
#   tree that rolled its children up would silently invent a different total than
#   the one the report files.
# * **The tag level appears only when it means something.** An item with an
#   explicit tag list always shows it, empty buckets included -- an empty bucket
#   IS the finding, as a property with no rent booked to it this year. An item
#   discovering its own tags shows the level only when its lines actually carry
#   tags, which is what keeps the default quiet.
# * **An excluded line is shown, not dropped.** It carries its real amount for
#   display and contributes nothing to any node above it.

@dataclass(frozen=True)
class DrillMark:
    """One amber-triangle warning on an item node, with the tooltip to show.

    The TEXT is built here rather than in the window because it is a statement
    about the arithmetic ("these two items share 31 lines"), and a rule the user
    can only read in a tooltip is still a rule -- it belongs where it can be
    tested without a running Qt."""
    code: str                   # 'shared_lines' | 'transfer_both_legs' | 'double_tagged'
    text: str


@dataclass(frozen=True)
class DrillLine:
    """One transaction (or split leg) under a category node.

    ``amount`` already carries the item's sign, exactly as every other amount in
    the report does, so a deduction reads positive here and in the row above it.
    ``excluded`` means an exclusion tag pushed this line out: it is drawn struck
    through and adds nothing to the totals. ``split_id`` addresses the leg for
    the exclusion toggle -- ``None`` for a whole transaction."""
    txn_id: int
    split_id: Optional[int]
    date: str
    payee: str
    memo: str
    account_id: int
    amount: int
    excluded: bool = False


@dataclass
class DrillNode:
    """One node of the drill-down. ``kind`` is ``item``/``tag``/``category``/``txn``.

    ``amount`` is signed cents with the item's sign applied. On an ``item`` node
    it is :func:`evaluate`'s own row, never a sum taken here."""
    kind: str
    label: str
    amount: int
    children: list = field(default_factory=list)
    item_id: Optional[int] = None
    item_name: str = ""
    item_kind: str = ""
    tag_value: Optional[str] = None
    category_id: Optional[int] = None
    line: Optional[DrillLine] = None
    marks: tuple = ()
    line_count: int = 0

    @property
    def excluded(self) -> bool:
        return self.line is not None and self.line.excluded


@dataclass(frozen=True)
class DrillTree:
    """The whole report as a tree, plus the range it was built over.

    There is NO grand total here, and that is a statement rather than an
    omission. One category legitimately feeds several tax lines -- state
    withholding is both a W-2 line and a Schedule A deduction -- so this report's
    items share money by design (``Coverage.multi_claimed`` is the same fact
    reported the other way round), and summing them measures nothing. No tax form
    asks for it either; a return is filed line by line. A report that wants the
    sum of particular lines says which, with a ``COMPUTED`` item."""
    report_id: int
    name: str
    start: str
    end: str
    items: tuple = ()


def drill_down(conn: sqlite3.Connection, report_id: int,
               start: Optional[str] = None, end: Optional[str] = None,
               *, today: Optional[_dt.date] = None) -> DrillTree:
    """The report as an explorable tree: line item, then its tags, then the
    categories under each, then the transactions themselves.

    Built on ``evaluate(detail=True)`` -- one evaluation, one set of numbers.
    Item nodes carry that evaluation's rows unchanged, so opening a line can
    never show a different total than closing it."""
    ev = evaluate(conn, report_id, start, end, today=today, detail=True)
    items = list_items(conn, report_id)
    rows = {row.item_id: row for row in ev.rows if not row.is_breakdown}
    paths = category_paths(conn)
    accounts = {int(a["id"]): a["name"] for a in ledger.list_accounts(
        conn, include_closed=True, include_hidden=True)}
    marks = _drill_marks(ev, items)

    nodes: list[DrillNode] = []
    for item in items:
        row = rows[item.id]
        node = DrillNode(
            kind="item", label=item.display_label, amount=int(row.amount),
            item_id=item.id, item_name=item.name, item_kind=item.kind,
            marks=marks.get(item.id, ()), line_count=int(row.line_count))
        detail = ev.details.get(item.id)
        if detail is not None and detail.parts:
            # A kind whose number is a DIFFERENCE explains itself with its terms
            # rather than with lines. They are not a partition and nothing sums
            # them -- the same rule every other level follows.
            node.children = [
                DrillNode(kind="part", label=label,
                          amount=int(cents) * int(item.sign),
                          item_id=item.id, item_name=item.name,
                          item_kind=item.kind)
                for label, cents in detail.parts]
        elif detail is not None:
            node.children = _drill_item_children(item, detail, paths, accounts)
        nodes.append(node)
    return DrillTree(report_id=ev.report_id, name=ev.name, start=ev.start,
                     end=ev.end, items=tuple(nodes))


def _drill_item_children(item: ReportItem, detail: ItemDetail,
                         paths: dict, accounts: dict) -> list:
    """One item's subtree: the tag level when it applies, categories under it.

    The bucketing repeats :func:`evaluate`'s exactly -- same ``_breakdown_tags``,
    same restrict list -- because the tag nodes have to agree with the breakdown
    sub-rows the export writes."""
    breakdown = item.break_by_tag
    folded = item.name.casefold() if item.tag_enabled else None
    if breakdown is None:                       # the breakdown is opted out
        return _drill_category_nodes(item, detail.admitted, detail.excluded,
                                     paths, accounts)
    restrict = ({n.casefold(): n for n in breakdown} or None
                if breakdown else None)
    buckets: dict[str, list] = {}               # casefold -> [display, admitted, excluded]
    rest_admitted: list[Line] = []
    rest_excluded: list[Line] = []
    for lines, slot in ((detail.admitted, 1), (detail.excluded, 2)):
        for line in lines:
            hits = _breakdown_tags(line, folded, restrict)
            for fold, display in hits:
                buckets.setdefault(fold, [display, [], []])[slot].append(line)
            if not hits:
                (rest_admitted if slot == 1 else rest_excluded).append(line)
    if breakdown:
        order = [(n.casefold(), n) for n in breakdown]
    elif buckets:
        order = sorted(((f, s[0]) for f, s in buckets.items()),
                       key=lambda pair: pair[0])
    else:
        # Discovery with nothing discovered: no tag level at all, the same way
        # ``_breakdown_rows`` emits no sub-rows. The categories move up a level.
        return _drill_category_nodes(item, detail.admitted, detail.excluded,
                                     paths, accounts)
    sign = int(item.sign)
    out: list[DrillNode] = []
    for fold, display in order:
        slot = buckets.get(fold)
        admitted = slot[1] if slot else []
        excluded = slot[2] if slot else []
        out.append(DrillNode(
            kind="tag", label=display, tag_value=display,
            amount=sum(int(ln.amount) for ln in admitted) * sign,
            item_id=item.id, item_name=item.name, item_kind=item.kind,
            line_count=len(admitted),
            children=_drill_category_nodes(item, admitted, excluded,
                                           paths, accounts)))
    # The remainder is always shown for an explicit list -- money with no property
    # on it is exactly what that report is looking for -- and only when it holds
    # something in discovery mode, where a lone "(untagged)" restating the total
    # is the noise the breakdown default was designed to avoid.
    if breakdown or rest_admitted or rest_excluded:
        out.append(DrillNode(
            kind="tag", label=UNTAGGED_LABEL, tag_value=None,
            amount=sum(int(ln.amount) for ln in rest_admitted) * sign,
            item_id=item.id, item_name=item.name, item_kind=item.kind,
            line_count=len(rest_admitted),
            children=_drill_category_nodes(item, rest_admitted, rest_excluded,
                                           paths, accounts)))
    return out


def _drill_category_nodes(item: ReportItem, admitted: Sequence[Line],
                          excluded: Sequence[Line], paths: dict,
                          accounts: dict) -> list:
    """The category level and its transactions, for one bucket of lines."""
    sign = int(item.sign)
    groups: dict[tuple, list] = {}              # (sort key, label) -> [admitted, excluded]
    for lines, slot in ((admitted, 0), (excluded, 1)):
        for line in lines:
            key = _drill_category_label(line, paths, accounts)
            groups.setdefault(key, [[], []])[slot].append(line)
    out: list[DrillNode] = []
    for (label, cid) in sorted(groups, key=lambda k: k[0].casefold()):
        keep, dropped = groups[(label, cid)]
        leaves = sorted(
            [(ln, False) for ln in keep] + [(ln, True) for ln in dropped],
            key=lambda pair: (pair[0].date, pair[0].txn_id,
                              pair[0].split_id or 0))
        out.append(DrillNode(
            kind="category", label=label, category_id=cid,
            amount=sum(int(ln.amount) for ln in keep) * sign,
            item_id=item.id, item_name=item.name, item_kind=item.kind,
            line_count=len(keep),
            children=[_drill_txn_node(item, ln, dropped_flag, sign)
                      for ln, dropped_flag in leaves]))
    return out


def _drill_category_label(line: Line, paths: dict, accounts: dict) -> tuple:
    """The category node a line belongs under, as ``(label, category_id)``.

    A transfer leg has no category -- it is shown as the bracketed counterparty
    account the register writes, which is what the user recognizes."""
    if line.transfer_account_id is not None:
        name = accounts.get(int(line.transfer_account_id), "?")
        return (f"[{name}]", None)
    if line.category_id is None:
        return ("(uncategorized)", None)
    return (paths.get(int(line.category_id), "?"), int(line.category_id))


def _drill_txn_node(item: ReportItem, line: Line, excluded: bool,
                    sign: int) -> DrillNode:
    return DrillNode(
        kind="txn", label=line.date,
        amount=int(line.amount) * sign,
        item_id=item.id, item_name=item.name, item_kind=item.kind,
        line_count=0 if excluded else 1,
        line=DrillLine(
            txn_id=int(line.txn_id), split_id=line.split_id, date=line.date,
            payee=line.payee or "", memo=line.memo or "",
            account_id=int(line.account_id), amount=int(line.amount) * sign,
            excluded=excluded))


def _drill_marks(ev: Evaluation, items: Sequence[ReportItem]) -> dict:
    """The amber triangles, per item id.

    Three causes, and each one means a number on screen is not what it looks
    like. That is the whole bar for earning a triangle: a mark that fires on
    something normal teaches the user to ignore marks."""
    by_name = {item.name: item for item in items}
    shared: dict[int, dict] = {}                # item id -> {other name: count}
    for claim in ev.coverage.multi_claimed:
        for name in claim.item_names:
            item = by_name.get(name)
            if item is None:
                continue
            others = shared.setdefault(item.id, {})
            for other in claim.item_names:
                if other != name:
                    others[other] = others.get(other, 0) + 1
    transfers: dict[int, int] = {}
    for claim in ev.coverage.transfer_double_counted:
        for name in claim.item_names:
            item = by_name.get(name)
            if item is not None:
                transfers[item.id] = transfers.get(item.id, 0) + 1

    out: dict[int, list] = {}
    for item in items:
        marks: list[DrillMark] = []
        others = shared.get(item.id)
        if others:
            names = ", ".join(sorted(others))
            lines = max(others.values())
            marks.append(DrillMark(
                "shared_lines",
                f"{lines} line{'' if lines == 1 else 's'} here are also claimed "
                f"by {names}. That is allowed -- one category can feed several "
                f"tax lines -- but it is why the items of this report do not sum "
                f"to a meaningful grand total."))
        count = transfers.get(item.id, 0)
        if count:
            marks.append(DrillMark(
                "transfer_both_legs",
                f"Both legs of {count} transfer{'' if count == 1 else 's'} are "
                f"pulled into this line by its tag, so the money is counted "
                f"twice. Remove the tag from one side."))
        doubled = _double_tagged_count(item, ev.details.get(item.id))
        if doubled:
            marks.append(DrillMark(
                "double_tagged",
                f"{doubled} line{'' if doubled == 1 else 's'} carry two of this "
                f"line's listed tags and are counted under each, so the tag "
                f"subtotals below add up to more than the line's own total. The "
                f"total is the correct figure."))
        if marks:
            out[item.id] = tuple(marks)
    return out


def _double_tagged_count(item: ReportItem, detail: Optional[ItemDetail]) -> int:
    """How many admitted lines carry TWO of an explicit tag list's tags.

    Only asked of an explicit list. In discovery mode the tag nodes are a re-cut
    of the money and everyone reading them knows it -- a rent that is also tagged
    ``late`` lands under both ``late`` and its property, which is odd to look at
    but breaks nothing. With a LIST the nodes are meant to partition, so two hits
    on one line means one payment booked to two properties: wrong data, rare, and
    worth a triangle."""
    breakdown = item.break_by_tag
    if not breakdown or detail is None:
        return 0
    restrict = {n.casefold(): n for n in breakdown}
    folded = item.name.casefold() if item.tag_enabled else None
    return sum(1 for line in detail.admitted
               if len(_breakdown_tags(line, folded, restrict)) >= 2)


def compare(conn: sqlite3.Connection, report_id: int,
            ranges: Sequence[tuple], *,
            today: Optional[_dt.date] = None) -> ComparisonResult:
    """Evaluate ONE definition over several ranges and align the rows.

    ``ranges`` is a sequence of ``(start, end, label)`` -- the label is optional
    and defaults to the year when the range is exactly a calendar year, which is
    the overwhelmingly common case ("2023" beside "2024" beside "2025").

    This is the payoff of storing a date range as a BINDING rather than as two
    dates baked into the items: re-pointing is just calling :func:`evaluate`
    again with a different range, so a column can never disagree with the
    single-range report about what the definition means.

    ``Coverage`` is computed PER COLUMN, because it is a statement about a
    range: a category that nobody posted to in 2023 leaves a different set of
    unclaimed lines than the same category in 2025. Rows are aligned by item,
    in ``seq`` order, and a column missing a row gets an explicit zero rather
    than a gap. A zero is not automatically a fact, though -- if a priced kind
    had no price on or before that column's end, the cell carries ``unpriced``
    and reads as INCOMPLETE."""
    wanted = [tuple(r) for r in ranges]
    if not wanted:
        raise ValueError("compare needs at least one range")
    rd = get_report(conn, report_id)
    items = list_items(conn, report_id)

    columns: list[ComparisonColumn] = []
    cells: list[dict[int, ReportRow]] = []
    for entry in wanted:
        if len(entry) == 3:
            start, end, label = entry
        elif len(entry) == 2:
            (start, end), label = entry, None
        else:
            raise ValueError(
                "each range must be (start, end) or (start, end, label)")
        ev = evaluate(conn, report_id, start, end, today=today)
        columns.append(ComparisonColumn(
            label=str(label) if label else _range_label(ev.start, ev.end),
            start=ev.start, end=ev.end, coverage=ev.coverage))
        cells.append({row.item_id: row for row in ev.rows})

    rows = [ComparisonRow(
        item_id=item.id, name=item.name, label=item.display_label,
        group_label=item.group_label, kind=item.kind, sign=int(item.sign),
        cells=tuple(col.get(item.id) or _row(item, 0, 0) for col in cells))
        for item in items]
    return ComparisonResult(report_id=rd.id, name=rd.name,
                            columns=tuple(columns), rows=tuple(rows))


def _range_label(start: str, end: str) -> str:
    """"2025" for a whole calendar year, otherwise the two dates."""
    if start[4:] == "-01-01" and end[4:] == "-12-31" and start[:4] == end[:4]:
        return start[:4]
    return f"{start} to {end}"


def _coverage(lines: Sequence[Line], claims: dict[int, list[str]],
              selected_any: set[int],
              doubles: Sequence[LineClaim] = (),
              ignored: Sequence[IgnoredTag] = ()) -> Coverage:
    """Assemble the coverage panel. ``unclaimed`` is a line the report was
    pointed at (its category is in the union of the selections) that no item
    ended up claiming -- only an exclusion tag can produce one, since a selected
    category otherwise claims its own lines."""
    multi: list[LineClaim] = []
    unclaimed: list[LineClaim] = []
    for idx, line in enumerate(lines):
        names = claims.get(idx, [])
        if len(names) >= 2:
            multi.append(_claim(line, names))
        elif not names and line.category_id in selected_any:
            unclaimed.append(_claim(line, names))
    return Coverage(multi_claimed=tuple(multi), unclaimed=tuple(unclaimed),
                    transfer_double_counted=tuple(doubles),
                    ignored_tags=tuple(ignored))


def _transfer_double_counts(conn: sqlite3.Connection, lines: Sequence[Line],
                            indexes: Sequence[int],
                            item_name: str) -> list[LineClaim]:
    """One warning per transfer whose BOTH legs an inclusion tag pulled into the
    same item -- the money counted twice.

    A pair is only believed when the two rows point at EACH OTHER through
    ``transfer_pair_id`` and their amounts exactly cancel; a stale one-way link
    is not evidence of a pair, and a coincidence of amounts is not either. A
    transfer that lives on a split LINE is therefore not detected here (the
    parent's link is NULL and only the mirror points back) -- deliberately
    conservative: a missing warning costs the user a second look, a false one
    costs him trust in every warning.
    """
    if len(indexes) < 2:
        return []
    first_index: dict[int, int] = {}
    for idx in indexes:
        first_index.setdefault(lines[idx].txn_id, idx)
    if len(first_index) < 2:
        return []
    marks = ",".join("?" for _ in first_index)
    meta = {
        int(r["id"]): (r["transfer_pair_id"], int(r["amount"]))
        for r in conn.execute(
            f"SELECT id, transfer_pair_id, amount FROM transactions "
            f"WHERE id IN ({marks})", tuple(first_index)).fetchall()}
    out: list[LineClaim] = []
    seen: set[int] = set()
    for txn_id, (pair, amount) in sorted(meta.items()):
        if txn_id in seen or pair is None:
            continue
        other = int(pair)
        if other not in meta:
            continue
        other_pair, other_amount = meta[other]
        if other_pair is None or int(other_pair) != txn_id:
            continue
        if amount + other_amount != 0:
            continue
        seen.add(txn_id)
        seen.add(other)
        out.append(_claim(lines[first_index[txn_id]], [item_name],
                          pair_txn_id=other))
    return out


def _claim(line: Line, names: Sequence[str],
           pair_txn_id: Optional[int] = None) -> LineClaim:
    return LineClaim(txn_id=line.txn_id, date=line.date,
                     account_id=line.account_id, category_id=line.category_id,
                     amount=int(line.amount), is_split_line=line.is_split_line,
                     item_names=tuple(names), pair_txn_id=pair_txn_id)


def _row(item: ReportItem, raw: int, count: int,
         unpriced: Sequence[str] = ()) -> ReportRow:
    return ReportRow(item_id=item.id, name=item.name, label=item.display_label,
                     group_label=item.group_label, kind=item.kind,
                     sign=int(item.sign), amount=int(raw) * int(item.sign),
                     raw_amount=int(raw), line_count=count,
                     unpriced=tuple(unpriced))


def _sub_row(item: ReportItem, tag_value: Optional[str], raw: int, count: int,
             copy: int, unpriced: Sequence[str] = ()) -> ReportRow:
    """One breakdown row of ``item``. ``tag_value`` is the tag it sums, or
    ``None`` for the remainder. The label is the TAG, not the item: a sub-row is
    read under its item, and the export wants the property name in the record.

    The sign is the item's, applied exactly as on the total, so a sub-row and
    the total it belongs to are read on the same footing."""
    return ReportRow(item_id=item.id, name=item.name,
                     label=tag_value if tag_value is not None else UNTAGGED_LABEL,
                     group_label=item.group_label, kind=item.kind,
                     sign=int(item.sign), amount=int(raw) * int(item.sign),
                     raw_amount=int(raw), line_count=count,
                     unpriced=tuple(unpriced), is_breakdown=True,
                     tag_value=tag_value, txf_copy=int(copy))


# What :func:`_admit` decided about one line, for one item.
ADMIT = "admit"
EXCLUDED = "excluded"
SKIP = "skip"


def _admit(line: Line, tags: set, wanted: set, own: Optional[str],
           negated: Optional[str], transfers: frozenset = frozenset()) -> str:
    """Whether ``line`` belongs to an SOSC item: the selection rules, in one
    place because two callers now ask the question.

    ELIGIBILITY is decided first (rules 2-4) and the exclusion applied to what
    survives. Exclusion used to be tested first and simply skipped the line,
    which conflated "this item lost a line it would have had" with "this line was
    never this item's business" -- the same answer for a total, two very
    different things for a user reading a drill-down.

    * rule 1 -- an exclusion tag always wins, over the item's own inclusion tag on
      the same line and over every selection below, with no way back in. It is
      honoured whether or not the item is ``tag_enabled`` (see :func:`evaluate`).
      On a split leg ``tags`` is the parent's set plus the leg's own, which is
      what makes "tag the parent, carve out one leg" work;
    * rule 2 -- an inclusion tag beats the selections, so a posting in a category
      this item never selected still joins;
    * rule 3 -- the category selection admits an ordinary posting;
    * rule 4 -- the TRANSFER selection admits a transfer leg, matched on the
      account at its FAR side.

    Rule 4 is what lets a tax line be stated net of money that merely moved. A
    401(k) deferral is not an expense and has no category -- it is a leg of the
    paycheck transferring to the retirement account -- so before this an item
    summing categories could not reach it at all, and W-2 box 1 wages (gross pay
    LESS the deferral) were not expressible. Matching on the FAR side is what
    makes it single-sided and safe: a transfer contributes two rows to
    ``signed_lines``, and only the one sitting outside the selected account
    points AT it, so selecting ``[401k]`` picks up the paycheck's leg and not its
    mirror. The leg enters at its OWN sign, exactly as a tagged transfer does, so
    a deferral of -453.11 reduces the salary it is added to. Selecting BOTH
    accounts of one transfer does match both rows; that is a real double count,
    and :func:`_transfer_double_counts` reports it rather than guessing.

    Returns :data:`ADMIT` (summed), :data:`EXCLUDED` (eligible, then pushed out by
    rule 1 -- the struck-through rows) or :data:`SKIP` (never this item's line).
    """
    included = own is not None and own in tags
    if not included and not (
            line.category_id is not None and line.category_id in wanted) \
            and not (line.transfer_account_id is not None
                     and line.transfer_account_id in transfers):
        return SKIP
    if negated is not None and negated in tags:
        return EXCLUDED
    return ADMIT


def _breakdown_tags(line: Line, own: Optional[str],
                    restrict: Optional[dict[str, str]]) -> list[tuple[str, str]]:
    """The buckets one already-admitted line falls into, as
    ``(casefold, spelling)`` in the order the line carries them.

    Dropped: an exclusion marker (``!foo`` is not a property) and, in
    discover-every-tag mode, ``own`` -- the case-folded name of THIS item when it
    is tag-enabled, i.e. the tag that admitted the line in the first place.
    Bucketing by it would answer "all of it" and dress that up as a property.
    Nothing else is suppressed: a report-wide set of every tag-enabled item's
    name used to be, and in the rental shape those names ARE the property tags,
    so every sub-row came out 0.00.
    With ``restrict`` set, only the listed tags bucket at all -- anything else
    leaves the line to the remainder row, so the sub-rows still account for every
    cent of the total. A LISTED tag is never dropped, not even ``own``: the user
    who typed it named a property. A line carrying two buckets lands in BOTH; see
    the module docstring for why that is the honest answer."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in ledger.parse_tags(line.tag):
        fold = name.casefold()
        if name.startswith("!") or fold in seen:
            continue
        if restrict is not None:
            if fold not in restrict:
                continue
        elif fold == own:
            continue
        seen.add(fold)
        out.append((fold, restrict[fold] if restrict is not None else name))
    return out


def _breakdown_rows(item: ReportItem, breakdown: Sequence[str],
                    buckets: dict[str, list], remainder: Sequence[int],
                    ) -> list[ReportRow]:
    """The sub-rows of a broken-down SOSC item, tags first and the remainder
    last.

    An EXPLICIT tag list is emitted in the order it was written, zero rows
    included: a property with no spending this year must keep its place, or every
    later property's TXF copy number would shift under it between filings.
    Otherwise the tags actually seen are emitted sorted by case-folded name, for
    an order that does not depend on which transaction was entered first.

    DISCOVERY with nothing to discover emits NO rows at all. The breakdown is on
    by default, so most items in most reports reach here with no tagged lines
    whatsoever, and a lone ``(untagged)`` row restating the total is noise -- in
    the TXF export it would also turn one record into two saying the same thing.
    An explicit list still emits its rows, zeros included, because the user asked
    for those buckets by name."""
    if not breakdown and not buckets:
        return []
    if breakdown:
        order = [(name.casefold(), name) for name in breakdown]
    else:
        order = sorted(((fold, slot[0]) for fold, slot in buckets.items()),
                       key=lambda pair: pair[0])
    base = int(item.txf_copy or 1)
    rows = []
    for offset, (fold, display) in enumerate(order):
        slot = buckets.get(fold)
        rows.append(_sub_row(item, display, slot[1] if slot else 0,
                             slot[2] if slot else 0, base + offset))
    rows.append(_sub_row(item, None, int(remainder[0]), int(remainder[1]),
                         base + len(order)))
    return rows


def _computed_breakdown(item: ReportItem, deps: Sequence[ReportItem],
                        by_name: dict[str, ReportItem],
                        sub_by_id: dict[int, list[ReportRow]],
                        ) -> list[ReportRow]:
    """The per-tag rows of a COMPUTED item, over the UNION of its referents' tag
    values -- so "interest + property tax + maintenance" comes out once per
    property without anyone restating the formula.

    The union is taken in referent order, first appearance winning the spelling,
    which makes the order a function of the report and not of the data. A
    referent with no breakdown (or none for this tag) reads as ZERO in the
    per-tag cells while still counting in full toward the total, because that is
    what it knows: it has not said which property its money belongs to. Its own
    ``break_by_tag`` list, if set, restricts and orders the union instead.
    Returns ``[]`` when no referent is broken down at all."""
    tag_rows: dict[int, dict[Optional[str], ReportRow]] = {}
    order: list[tuple[str, str]] = []
    seen: set[str] = set()
    for dep in deps:
        subs = sub_by_id.get(dep.id)
        if not subs:
            continue
        tag_rows[dep.id] = {row.tag_value: row for row in subs}
        for row in subs:
            if row.tag_value is None:
                continue
            fold = row.tag_value.casefold()
            if fold not in seen:
                seen.add(fold)
                order.append((fold, row.tag_value))
    if not tag_rows:
        return []
    restrict = item.break_by_tag
    if restrict:
        order = [(name.casefold(), name) for name in restrict]

    def cell(tag_value: Optional[str], fold: Optional[str]) -> int:
        def resolve(name: str) -> Decimal:
            rows = tag_rows.get(by_name[name].id)
            if not rows:
                return Decimal(0)
            if fold is None:
                row = rows.get(None)
            else:
                row = next((r for key, r in rows.items()
                            if key is not None and key.casefold() == fold), None)
            return Decimal(row.amount if row is not None else 0)
        value = _eval_expr(item.expr, resolve)
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    base = int(item.txf_copy or 1)
    rows = []
    for offset, (fold, display) in enumerate(order):
        rows.append(_sub_row(item, display, cell(display, fold), len(deps),
                             base + offset))
    rows.append(_sub_row(item, None, cell(None, None), len(deps),
                         base + len(order)))
    return rows


def _selected_category_ids(conn: sqlite3.Connection, item_id: int) -> set[int]:
    """The category ids an SOSC item matches, expanded FRESH.

    Every stored id is in the set -- a partially checked parent included, or its
    subtree would vanish -- and every subtree-flagged id additionally brings its
    descendants, computed now rather than snapshotted, so a category added under
    ``Rental`` next year needs no edit to the item."""
    stored = item_categories(conn, item_id)
    wanted = {cid for cid, _ in stored}
    subtree_roots = [cid for cid, sub in stored if sub]
    if subtree_roots:
        wanted |= category_subtree(conn, subtree_roots)
    return wanted


def _selected_transfer_ids(conn: sqlite3.Connection, item_id: int) -> frozenset:
    """The accounts an SOSC item matches TRANSFERS to, as a frozenset.

    Stored in ``report_item_accounts`` -- the same table the balance kinds use
    for "which accounts to value". Reusing it needs no migration (migrations are
    append-only, and an SOSC item never had a row there), and the two readings
    cannot collide because an item has exactly one kind. The separate name is the
    guard: a reader lands on the meaning its kind gives the rows, rather than on
    whichever accessor came to hand."""
    return frozenset(item_accounts(conn, item_id))


def _display_balance(conn: sqlite3.Connection, account_id: int, as_of: str) -> int:
    """``investments.display_balance`` -- imported lazily, because it pulls in
    the valuation machinery and nothing else in this module needs it."""
    from mammon import investments
    return int(investments.display_balance(conn, account_id, as_of=as_of))


def _unpriced_symbols(conn: sqlite3.Connection, account_ids: Sequence[int],
                      as_of: str) -> tuple[str, ...]:
    """The symbols these accounts HELD on ``as_of`` with no price on or before
    it, sorted.

    This is the difference between "worth nothing" and "not known": a balance
    that silently omits an unpriced holding reads as a confident number, and the
    user has no way to tell it apart from a real zero. Returning the names lets
    the row say WHICH security it could not value."""
    from mammon import investments
    missing: set[str] = set()
    for aid in account_ids:
        if _is_crypto_account(conn, aid):
            # Same blind spot as :func:`_holdings_value`, and worse here: an
            # EDAB/SDAB/NETGAIN row over a crypto wallet DOES value correctly
            # (``_display_balance`` routes to the crypto layer), so a coin with
            # no price on the as-of date silently drops out of a number that
            # then reads as a confident zero. This is what makes it say so.
            from mammon import crypto
            missing.update(crypto.account_valuation(conn, aid, as_of).unpriced)
            continue
        for symbol, lot in investments.compute_holdings(
                conn, aid, as_of=as_of).items():
            if lot.qty == 0 or symbol in missing:
                continue
            if investments.latest_price(conn, symbol, as_of=as_of) is None:
                missing.add(symbol)
    return tuple(sorted(missing))


def _is_crypto_account(conn: sqlite3.Connection, account_id: int) -> bool:
    """Whether this account's holdings live in the crypto_* tables.

    The same test ``investments.display_balance`` makes before handing a wallet
    to the crypto domain layer. It is asked here too because the report's
    holdings helpers reach for the securities tables directly, and those answer
    "nothing held" for a wallet rather than raising."""
    acct = ledger.get_account(conn, account_id)
    return acct is not None and (acct["type"] or "") == "crypto"


def _holdings_value(conn: sqlite3.Connection, account_ids: Sequence[int],
                    symbols: Sequence[str],
                    as_of: str) -> tuple[int, tuple[str, ...], int]:
    """``(cents, unpriced symbols, positions counted)`` for a HOLDVAL item.

    An EMPTY ``symbols`` selection means every symbol held, not none -- the
    common case is "what is this account worth", and making that require naming
    each holding would go stale the day a new one is bought. Valuation is
    delegated to ``investments.holding_values_at`` so a report and the holdings
    screen round identically; a position held at a non-zero quantity that comes
    back with no value is exactly an unpriced one."""
    from mammon import investments
    wanted = {s.strip().upper() for s in symbols if (s or "").strip()}
    total = 0
    missing: set[str] = set()
    count = 0
    for aid in account_ids:
        if _is_crypto_account(conn, aid):
            # A crypto wallet's coins live in the crypto_* tables, so the
            # securities helpers see no positions and this read 0.00 -- with no
            # unpriced warning either, because zero positions cannot be unpriced.
            # A silent zero is the one answer a valuation must never give.
            from mammon import crypto
            for held in crypto.account_valuation(conn, aid, as_of).holdings:
                if not held.quantity:
                    continue
                if wanted and held.symbol.upper() not in wanted:
                    continue
                count += 1
                if held.price is None:
                    missing.add(held.symbol)
                else:
                    total += int(held.market_value)
            continue
        held_lots = investments.compute_holdings(conn, aid, as_of=as_of)
        values = investments.holding_values_at(conn, aid, as_of=as_of)
        for symbol, lot in held_lots.items():
            if lot.qty == 0:
                continue
            if wanted and symbol.upper() not in wanted:
                continue
            count += 1
            if symbol in values:
                total += int(values[symbol])
            else:
                missing.add(symbol)
    return total, tuple(sorted(missing)), count


def _realized_gain(conn: sqlite3.Connection, item: ReportItem,
                   start: str, end: str) -> tuple[int, int]:
    """``(cents, lots counted)`` for a RGAIN item over the sale dates in range.

    ``options["term"]`` selects ``short``/``long``/``all``; ``all`` is the
    default and deliberately includes a lot whose holding period could not be
    determined, because dropping it would quietly understate the total."""
    from mammon import portfolio
    term = (item.options_dict().get("term") or "all")
    if term not in RGAIN_TERMS:
        raise ValueError(
            f"item {item.name!r}: term must be one of {RGAIN_TERMS}")
    wanted = {s.strip().upper() for s in item_securities(conn, item.id)
              if (s or "").strip()}
    total = 0
    count = 0
    proceeds = 0
    basis = 0
    for aid in item_accounts(conn, item.id):
        for gain in portfolio.capital_gains(conn, aid, start=start, end=end):
            if wanted and gain.symbol.upper() not in wanted:
                continue
            if term != "all" and gain.term != term:
                continue
            total += int(gain.gain)
            proceeds += int(gain.proceeds)
            basis += int(gain.basis)
            count += 1
    return total, count, proceeds, basis


def _external_flow_total(conn: sqlite3.Connection, account_ids: Sequence[int],
                         start: str, end: str) -> int:
    """Net cents the outside world put INTO these accounts during the range."""
    from mammon import portfolio
    return sum(int(amount)
               for aid in account_ids
               for _date, amount in portfolio.external_flows(conn, aid, start, end))


def _all_account_ids(conn: sqlite3.Connection) -> list[int]:
    """Every account, closed ones included: a closed account's history is still
    part of the year a report covers."""
    return [int(r["id"]) for r in conn.execute(
        "SELECT id FROM accounts ORDER BY id").fetchall()]


def _day_before(iso: str) -> str:
    validate_date(iso)
    return (_dt.date.fromisoformat(iso) - _dt.timedelta(days=1)).isoformat()


def _validate_range(range_kind: str, range_start, range_end, range_year,
                    range_preset) -> None:
    if range_kind not in RANGE_KINDS:
        raise ValueError(f"range_kind must be one of {RANGE_KINDS}")
    if range_start:
        validate_date(range_start)
    if range_end:
        validate_date(range_end)
    if range_kind == "fixed" and range_start and range_end and range_start > range_end:
        raise ValueError(f"start {range_start} is after end {range_end}")


def _normalize_category_selections(selections) -> list[tuple[int, int]]:
    if isinstance(selections, dict):
        pairs = list(selections.items())
    else:
        pairs = []
        for entry in selections:
            if isinstance(entry, (tuple, list)):
                cid, sub = entry
            else:
                cid, sub = entry, 0
            pairs.append((cid, sub))
    out: dict[int, int] = {}
    for cid, sub in pairs:
        cid = int(cid)
        out[cid] = max(out.get(cid, 0), 1 if sub else 0)
    return sorted(out.items())


def _def_from_row(row) -> ReportDef:
    return ReportDef(**{k: row[k] for k in _DEF_COLUMNS})


def _item_from_row(row) -> ReportItem:
    return ReportItem(**{k: row[k] for k in _ITEM_COLUMNS})
