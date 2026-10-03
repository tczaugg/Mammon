"""Year-definition files: build a report from a form's published line list.

A tax form is the same shape every year with different line NUMBERS, and the
work the user does on a tax report -- deciding which of his categories feed
line 8z -- is the expensive part. Re-entering thirty lines every January, and
re-deciding thirty category assignments, is how a tax report stops being used.
So the LINE LIST is data, not code: a small file per form-year, and two
operations over it.

* **Create Tax Report** (:func:`create_from_definition`) turns a definition into
  a report with every line present and NO selections. It evaluates to all zeros
  on purpose -- the unassigned lines are the to-do list
  (:func:`unassigned_items`), and a report that guessed at assignments would be
  a report nobody audits.
* **Update Report** (:func:`update_report`) builds next year's report from next
  year's definition and carries the user's selections across by following each
  line's ``migrated_from`` pointer. It NEVER mutates the source: last year's
  return is filed, and a migration that edited it in place would rewrite a
  number the user already sent to a tax authority.

Three shapes are load-bearing:

**JSON is canonical; YAML is optional.** ``requirements.txt`` has no PyYAML and
no optional extras, and a line-list loader is not worth making one. ``.json`` is
read with the stdlib; ``.yaml``/``.yml`` load only if ``import yaml`` happens to
succeed and otherwise say so plainly instead of dying on an ImportError.

**A kind change refuses rather than coerces.** If last year's line was ``SOSC``
(a sum over categories) and this year's successor is ``RGAIN`` (realized gain),
the category selection is not merely stale, it is meaningless -- so it is
reported as ``kind_changed`` and nothing is copied. Likewise a line that
DISAPPEARED is reported with its full selection spelled out in names, because
the user's real question is "where do those categories go now?" and making him
reopen last year's report to find out is how selections get silently lost.

No real tax data is checked in: the fixtures are a made-up ``FORM-X`` with
made-up line numbers.

This module writes only the ``report_*`` tables, through
:mod:`mammon.reports.custom`; it never touches transaction rows.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from mammon import ledger, paths
from mammon.reports import custom

# Version of the FILE format, not of the database schema. Bumped only if the
# key names below change meaning; an unknown version is refused loudly rather
# than half-understood.
FORMAT_VERSIONS = (1,)

SUFFIXES = (".json", ".yaml", ".yml")


# --------------------------------------------------------------------------
# What a definition file parses into
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DefinitionItem:
    """One line of a form, as the file states it.

    ``sign`` and ``tag_enabled`` are ``None`` when the file did not state them,
    which is the difference between "the form says this line is a subtraction"
    and "the file is silent, so keep whatever the user chose last year"
    (:func:`update_report` copies only the silent ones).

    ``migrated_from`` is normalized to ``(definition_id_or_None, name)`` or
    ``None``; the three source spellings are described in the module's design
    (a bare string, an explicit ``{definition, name}`` object, or absent).

    ``break_by_tag`` states that the line is reported once per TAG -- the
    Schedule E case, one copy per rental property. ``None`` is silence (the file
    predates the key, or says ``false``), ``()`` is "every tag the matched money
    carries", and a tuple restricts and ORDERS the properties. It is normalized
    here and stored in ``options`` by :func:`create_from_definition`, which is
    where :attr:`custom.ReportItem.break_by_tag` reads it back from."""
    name: str
    kind: str
    label: Optional[str] = None
    group_label: Optional[str] = None
    seq: Optional[int] = None
    sign: Optional[int] = None
    tag_enabled: Optional[bool] = None
    options: Optional[dict] = None
    expr: Optional[str] = None
    txf_refnum: Optional[int] = None
    txf_copy: int = 1
    txf_format: Optional[int] = None
    migrated_from: Optional[tuple[Optional[str], str]] = None
    break_by_tag: Optional[tuple[str, ...]] = None


@dataclass(frozen=True)
class Definition:
    """A parsed year-definition file."""
    id: str
    title: str
    items: tuple[DefinitionItem, ...]
    family: Optional[str] = None
    kind: str = "tax"
    year: Optional[int] = None
    default_range: Optional[dict] = None
    source: Optional[Path] = None

    def item(self, name: str) -> DefinitionItem:
        for it in self.items:
            if it.name == name:
                return it
        raise KeyError(name)


# --------------------------------------------------------------------------
# What a migration produced
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Carried:
    """A line whose predecessor was found and whose selections were copied."""
    name: str
    from_name: str
    n_categories: int = 0
    n_accounts: int = 0
    n_securities: int = 0


@dataclass(frozen=True)
class Dropped:
    """A source line with no successor, WITH its selection spelled out.

    The names, not the ids: the user is being asked "this no longer exists --
    where does Charitable Contributions go now?", and an id cannot be answered."""
    name: str
    kind: str
    label: Optional[str] = None
    categories: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    securities: tuple[str, ...] = ()


@dataclass(frozen=True)
class KindChanged:
    """A predecessor was found but the two lines compute different things, so
    nothing was copied."""
    name: str
    from_name: str
    from_kind: str
    to_kind: str


@dataclass(frozen=True)
class Unresolved:
    """A pointer that named something that is not there.

    Either a ``migrated_from`` naming a line the source report does not have
    (the definition was written against a different year), or a ``COMPUTED``
    expression referencing a brace-name that no longer exists. Both are
    reported, never raised and never silently zeroed: a formula that quietly
    evaluates a missing term as 0 is the worst outcome on a tax report."""
    name: str
    ref: str
    reason: str


@dataclass(frozen=True)
class MigrationResult:
    """Everything :func:`update_report` did and could not do, for rendering."""
    carried: tuple[Carried, ...] = ()
    new_lines: tuple[str, ...] = ()
    dropped: tuple[Dropped, ...] = ()
    kind_changed: tuple[KindChanged, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()

    @property
    def clean(self) -> bool:
        """True when nothing needs the user's attention: every line either
        carried or is honestly new."""
        return not (self.dropped or self.kind_changed or self.unresolved)


# --------------------------------------------------------------------------
# Finding and loading definitions
# --------------------------------------------------------------------------

def search_roots() -> list[Path]:
    """Where definitions are looked for, most-specific first: the user's own
    directory, then the one shipped with the install."""
    return [paths.report_defs_dir(),
            paths.install_root() / "mammon" / "report_defs"]


def find_definition_file(definition_id: str) -> Path:
    """The file backing ``definition_id``; the user's copy wins."""
    for root in search_roots():
        for suffix in SUFFIXES:
            candidate = root / f"{definition_id}{suffix}"
            if candidate.is_file():
                return candidate
    roots = ", ".join(str(r) for r in search_roots())
    raise FileNotFoundError(
        f"no report definition {definition_id!r} in any of: {roots}")


def list_definitions() -> list[Path]:
    """Every definition file visible, user root first, shadowed names dropped.

    Paths, not parsed definitions: a picker wants the list cheaply and a broken
    file in the directory must not stop the others being offered."""
    seen: set[str] = set()
    out: list[Path] = []
    for root in search_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if path.suffix.lower() not in SUFFIXES or not path.is_file():
                continue
            if path.stem in seen:
                continue
            seen.add(path.stem)
            out.append(path)
    return out


def load_definition(source: str | Path) -> Definition:
    """Load and validate a definition, by id or by explicit path.

    A bare ``'example-formx-2025'`` is looked up in the two search roots; a
    ``Path``, or a string carrying a suffix or a separator, is read as given (so
    a test or an import dialog can point at a file nobody installed)."""
    path = _as_path(source)
    raw = _read_mapping(path)
    return _parse(raw, path)


def _as_path(source: str | Path) -> Path:
    if isinstance(source, Path):
        return source
    text = str(source)
    if text.lower().endswith(SUFFIXES) or os.sep in text or "/" in text:
        return Path(text)
    return find_definition_file(text)


def _read_mapping(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml                                  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                f"{path.name} is YAML, and PyYAML is not installed. Mammon "
                "requires no optional packages: install PyYAML or convert the "
                "definition to JSON (the canonical format).") from exc
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    elif suffix == ".json":
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raise ValueError(
            f"{path.name}: a report definition must be one of {SUFFIXES}")
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: expected an object at the top level")
    return raw


def _parse(raw: dict, path: Optional[Path]) -> Definition:
    where = path.name if path is not None else "<definition>"
    version = raw.get("version", 1)
    if version not in FORMAT_VERSIONS:
        raise ValueError(
            f"{where}: definition format version {version!r} is not one of "
            f"{FORMAT_VERSIONS}; this copy of Mammon cannot read it")
    ident = str(raw.get("id") or (path.stem if path is not None else "")).strip()
    if not ident:
        raise ValueError(f"{where}: a definition needs an 'id'")
    kind = str(raw.get("kind") or "tax").strip()
    if kind not in custom.REPORT_KINDS:
        raise ValueError(
            f"{where}: kind must be one of {custom.REPORT_KINDS}")
    year = raw.get("year")
    entries = raw.get("items")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{where}: a definition needs a non-empty 'items' list")

    items: list[DefinitionItem] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: item {index} is not an object")
        item = _parse_item(entry, where, index)
        if item.name in seen:
            raise ValueError(f"{where}: two items named {item.name!r}")
        seen.add(item.name)
        items.append(item)

    default_range = raw.get("default_range")
    if default_range is None and year is not None:
        default_range = {"kind": "calendar_year", "year": int(year)}
    if default_range is not None:
        _range_kwargs(default_range, where)       # validate early, fail loudly

    return Definition(
        id=ident,
        title=str(raw.get("title") or ident),
        items=tuple(items),
        family=(str(raw["family"]).strip() if raw.get("family") else None),
        kind=kind,
        year=(int(year) if year is not None else None),
        default_range=default_range,
        source=path,
    )


def _parse_item(entry: dict, where: str, index: int) -> DefinitionItem:
    name = str(entry.get("name") or "").strip()
    if not name:
        raise ValueError(f"{where}: item {index} has no 'name'")
    kind = str(entry.get("kind") or "").strip().upper()
    if kind not in custom.ALL_KINDS:
        raise ValueError(
            f"{where}: item {name!r} has kind {kind!r}, not one of "
            f"{custom.ALL_KINDS}")
    sign = entry.get("sign")
    if sign is not None:
        sign = int(sign)
        if sign not in (1, -1):
            raise ValueError(f"{where}: item {name!r} sign must be +1 or -1")
    options = entry.get("options")
    if options is not None and not isinstance(options, dict):
        raise ValueError(f"{where}: item {name!r} 'options' must be an object")
    export = entry.get("export") or {}
    if not isinstance(export, dict):
        raise ValueError(f"{where}: item {name!r} 'export' must be an object")
    tag_enabled = entry.get("tag_enabled")
    seq = entry.get("seq")
    return DefinitionItem(
        name=name,
        kind=kind,
        label=(str(entry["label"]) if entry.get("label") else None),
        group_label=(str(entry["group"]) if entry.get("group") else None),
        seq=(int(seq) if seq is not None else None),
        sign=sign,
        tag_enabled=(bool(tag_enabled) if tag_enabled is not None else None),
        options=options,
        expr=(str(entry["expr"]) if entry.get("expr") else None),
        txf_refnum=_opt_int(export.get("txf_refnum")),
        txf_copy=int(export.get("txf_copy") or 1),
        txf_format=_opt_int(export.get("txf_format")),
        migrated_from=_parse_migrated_from(entry.get("migrated_from"),
                                           where, name),
        break_by_tag=_parse_break_by_tag(entry.get("break_by_tag"),
                                         where, name),
    )


def _parse_break_by_tag(value, where: str,
                        name: str) -> Optional[tuple[str, ...]]:
    """Normalize the item-level ``break_by_tag`` key.

    Absent or ``false`` is ``None`` (no breakdown), ``true`` is ``()`` (every
    tag the matched money carries), and a string or list of strings restricts
    the breakdown to those tags IN THAT ORDER -- which is what pins a rental
    property to the same TXF copy from one filing year to the next. A number or
    an object is a typo, not a shorthand, so it raises rather than guessing."""
    if value is None or value is False:
        return None
    if value is True:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"{where}: item {name!r} 'break_by_tag' must be true, false, or a "
            "list of tag names")
    names: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(
                f"{where}: item {name!r} 'break_by_tag' lists {entry!r}, "
                "which is not a tag name")
        text = entry.strip()
        # Keyed by casefold because ``tags.name`` is COLLATE NOCASE: two
        # spellings are one tag, and keeping both would put the same money on
        # two TXF copies.
        if text:
            names.setdefault(text.casefold(), text)
    return tuple(names.values())


def _parse_migrated_from(value, where: str,
                         name: str) -> Optional[tuple[Optional[str], str]]:
    """Normalize the three spellings into ``(definition_id_or_None, name)``."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return (None, text) if text else None
    if isinstance(value, dict):
        old = str(value.get("name") or "").strip()
        if not old:
            raise ValueError(
                f"{where}: item {name!r} migrated_from object needs a 'name'")
        definition = value.get("definition")
        return (str(definition).strip() if definition else None, old)
    if isinstance(value, list):
        # Deliberately v1-unsupported: a many-to-one merge needs a rule for
        # conflicting signs and kinds, and the honest answer is to ask the user.
        raise ValueError(
            f"{where}: item {name!r} migrated_from lists several predecessors; "
            "merges are not supported -- leave it absent and reassign the "
            "dropped lines by hand")
    raise ValueError(f"{where}: item {name!r} has an unreadable migrated_from")


def _opt_int(value) -> Optional[int]:
    return None if value is None else int(value)


# --------------------------------------------------------------------------
# Create Tax Report
# --------------------------------------------------------------------------

def create_from_definition(conn: sqlite3.Connection,
                           definition: str | Path | Definition, *, name: str,
                           range_override: Optional[dict] = None) -> int:
    """Create a report holding every line of ``definition`` and NO selections.

    The new report evaluates to all zeros; that is the point. Every line is
    listed by :func:`unassigned_items`, which is the to-do list the user works
    down. Nothing is guessed, because a guessed category assignment on a tax
    report is a wrong number nobody looks at twice."""
    defn = (definition if isinstance(definition, Definition)
            else load_definition(definition))
    rng = _range_kwargs(range_override or defn.default_range,
                        defn.source.name if defn.source else defn.id)
    report_id = custom.create_report(conn, name, kind=defn.kind,
                                     definition_id=defn.id, **rng)
    try:
        for position, item in enumerate(defn.items):
            custom.add_item(
                conn, report_id, item.name, item.kind,
                label=item.label, group_label=item.group_label,
                seq=(item.seq if item.seq is not None else position),
                sign=(item.sign if item.sign is not None else 1),
                tag_enabled=1 if item.tag_enabled else 0,
                options=_item_options(item), expr=item.expr,
                txf_refnum=item.txf_refnum, txf_copy=item.txf_copy,
                txf_format=item.txf_format)
    except Exception:
        # Never leave a half-built form behind: a report missing line 9 looks
        # complete and silently under-reports.
        custom.delete_report(conn, report_id)
        raise
    return report_id


def _item_options(item: DefinitionItem) -> Optional[dict]:
    """The item's stored ``options`` blob, with ``break_by_tag`` folded in.

    The per-tag breakdown rides ``options`` rather than a column of its own, so
    a file that never mentions it stores exactly what it stored before. A file
    that does gets the key added WITHOUT disturbing whatever else ``options``
    already says -- the dict is copied, because a Definition is shared and
    mutating it here would leak into the next report built from it."""
    if item.break_by_tag is None:
        return item.options
    options = dict(item.options or {})
    options[custom.BREAK_BY_TAG_OPTION] = (list(item.break_by_tag)
                                           if item.break_by_tag else True)
    return options


def _range_kwargs(spec: Optional[dict], where: str) -> dict:
    """Turn a definition's ``default_range`` into ``create_report`` kwargs."""
    if spec is None:
        raise ValueError(
            f"{where}: no 'default_range' and no 'year' to derive one from")
    if not isinstance(spec, dict):
        raise ValueError(f"{where}: 'default_range' must be an object")
    kind = str(spec.get("kind") or "").strip()
    if kind not in custom.RANGE_KINDS:
        raise ValueError(
            f"{where}: range kind must be one of {custom.RANGE_KINDS}")
    if kind == "calendar_year":
        if spec.get("year") is None:
            raise ValueError(f"{where}: a calendar_year range needs a 'year'")
        return {"range_kind": kind, "range_year": int(spec["year"])}
    if kind == "fixed":
        start, end = spec.get("start"), spec.get("end")
        if not (start and end):
            raise ValueError(f"{where}: a fixed range needs 'start' and 'end'")
        return {"range_kind": kind, "range_start": str(start),
                "range_end": str(end)}
    preset = spec.get("preset")
    if not preset:
        raise ValueError(f"{where}: a preset range needs a 'preset'")
    return {"range_kind": kind, "range_preset": str(preset)}


def unassigned_items(conn: sqlite3.Connection, report_id: int) -> list[str]:
    """The names of items with nothing selected yet -- the to-do list of a
    freshly created tax report, in display order.

    ``custom.Coverage`` answers a different question (which of the money the
    report was POINTED at fell out of it again), and on a report with no
    selections at all it is empty by construction. This is the other half: the
    lines that are still zero because nobody has told them what to sum."""
    out: list[str] = []
    for item in custom.list_items(conn, report_id):
        if item.kind == "COMPUTED":
            continue                       # its inputs are other items, not rows
        if (custom.item_categories(conn, item.id)
                or custom.item_accounts(conn, item.id)
                or _item_securities(conn, item.id)):
            continue
        out.append(item.name)
    return out


# --------------------------------------------------------------------------
# Update Report
# --------------------------------------------------------------------------

def update_report(conn: sqlite3.Connection, source_report_id: int,
                  definition: str | Path | Definition, *,
                  name: str) -> tuple[int, MigrationResult]:
    """Build next year's report from ``definition``, carrying selections over.

    Returns ``(new_report_id, MigrationResult)``. The source report is never
    touched -- last year's numbers were filed and stay exactly as filed. What
    could not be carried is REPORTED, in names the user can act on, rather than
    guessed at."""
    source = custom.get_report(conn, source_report_id)   # KeyError if it is gone
    defn = (definition if isinstance(definition, Definition)
            else load_definition(definition))
    old_items = {item.name: item for item in
                 custom.list_items(conn, source_report_id)}

    new_id = create_from_definition(conn, defn, name=name)
    new_items = {item.name: item for item in custom.list_items(conn, new_id)}

    carried: list[Carried] = []
    new_lines: list[str] = []
    kind_changed: list[KindChanged] = []
    unresolved: list[Unresolved] = []
    claimed: set[str] = set()

    for d_item in defn.items:
        target = new_items[d_item.name]
        ref = d_item.migrated_from
        if ref is None:
            new_lines.append(d_item.name)
            continue
        _, old_name = ref
        old = old_items.get(old_name)
        if old is None:
            unresolved.append(Unresolved(
                d_item.name, old_name,
                f"report {source.name!r} has no line named {old_name!r}"))
            continue
        claimed.add(old_name)
        if old.kind != d_item.kind:
            kind_changed.append(KindChanged(d_item.name, old_name,
                                            old.kind, d_item.kind))
            continue
        carried.append(_carry(conn, old, target, d_item))

    dropped = tuple(_dropped(conn, old) for old_name, old in old_items.items()
                    if old_name not in claimed)
    unresolved.extend(_unresolved_expr_refs(conn, new_id))

    return new_id, MigrationResult(
        carried=tuple(carried), new_lines=tuple(new_lines), dropped=dropped,
        kind_changed=tuple(kind_changed), unresolved=tuple(unresolved))


def _carry(conn: sqlite3.Connection, old: custom.ReportItem,
           target: custom.ReportItem, d_item: DefinitionItem) -> Carried:
    """Copy one line's SELECTIONS (never its amounts) onto its successor."""
    categories = custom.item_categories(conn, old.id)
    accounts = custom.item_accounts(conn, old.id)
    securities = _item_securities(conn, old.id)
    if categories:
        custom.set_item_categories(conn, target.id, categories)
    if accounts:
        custom.set_item_accounts(conn, target.id, accounts)
    if securities:
        _set_item_securities(conn, target.id, securities)

    # The definition wins on anything it states; the user's last answer stands
    # on anything it left silent.
    fields: dict[str, Any] = {}
    if d_item.sign is None and old.sign != target.sign:
        fields["sign"] = old.sign
    if d_item.tag_enabled is None and old.tag_enabled and not target.tag_enabled:
        try:
            ledger.validate_report_item_tag_name(conn, target.name,
                                                 item_id=target.id)
        except ValueError:
            # A tag name is unique LEDGER-wide, so while last year's report
            # still exists it keeps the tag. Leaving the flag off is the honest
            # outcome; turning it on would make two items fight over one tag.
            pass
        else:
            fields["tag_enabled"] = 1
    if fields:
        custom.update_item(conn, target.id, **fields)

    return Carried(target.name, old.name, len(categories), len(accounts),
                   len(securities))


def _dropped(conn: sqlite3.Connection, old: custom.ReportItem) -> Dropped:
    return Dropped(
        name=old.name, kind=old.kind, label=old.label,
        categories=tuple(ledger.category_path(conn, cid)
                         for cid, _sub in custom.item_categories(conn, old.id)),
        accounts=tuple(_account_name(conn, aid)
                       for aid in custom.item_accounts(conn, old.id)),
        securities=tuple(_item_securities(conn, old.id)),
    )


_BRACE_NAME = re.compile(r"\{([^{}]+)\}")


def _unresolved_expr_refs(conn: sqlite3.Connection,
                          report_id: int) -> list[Unresolved]:
    """COMPUTED expressions carry across verbatim, so a brace-name that the new
    year renumbered is a broken formula -- surfaced, never silently zeroed."""
    items = custom.list_items(conn, report_id)
    names = {item.name for item in items}
    out: list[Unresolved] = []
    for item in items:
        if item.kind != "COMPUTED" or not item.expr:
            continue
        for ref in _BRACE_NAME.findall(item.expr):
            if ref.strip() not in names:
                out.append(Unresolved(
                    item.name, ref.strip(),
                    f"expression references {{{ref.strip()}}}, which this "
                    "report has no line named"))
    return out


def _account_name(conn: sqlite3.Connection, account_id: int) -> str:
    row = ledger.get_account(conn, account_id)
    return str(row["name"]) if row is not None else f"account {account_id}"


def _item_securities(conn: sqlite3.Connection, item_id: int) -> list[str]:
    return [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM report_item_securities WHERE item_id = ? "
        "ORDER BY symbol", (item_id,))]


def _set_item_securities(conn: sqlite3.Connection, item_id: int,
                         symbols: Sequence[str]) -> None:
    conn.execute("DELETE FROM report_item_securities WHERE item_id = ?",
                 (item_id,))
    conn.executemany(
        "INSERT INTO report_item_securities (item_id, symbol) VALUES (?, ?)",
        [(item_id, s) for s in symbols])
    conn.commit()
