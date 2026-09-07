"""Session-scoped Undo/Redo for the account register.

WHY THIS SHAPE. Mammon's cast-iron rule is that ``mammon.ledger`` is the ONLY
writer of transaction rows, so every transfer/split/checkpoint invariant is
enforced in exactly one place. Undo/redo must not become a second write path.
So this module never touches SQL: it captures a *snapshot* of the affected
transaction(s) and REPLAYS the inverse THROUGH the same ledger functions the
forward edit used -- ``add_transaction`` / ``update_transaction`` /
``delete_transaction`` / ``create_transfer`` / ``set_splits`` / ``clear_splits``.
An undo of a transfer therefore inverts BOTH mirror legs together for free,
because the ledger functions already do (delete deletes both sides;
``update_transaction`` mirrors date/amount/payee/memo to the pair).

The stack is in-memory and lives for the session only -- there is no schema
change and nothing is persisted. It hangs off a register's ``RegisterModel``
(one history per open register), and the model's write chokepoints record into
it. The UI layer stays a thin projection: it calls ``capture``/``record_*`` and
``undo``/``redo`` here; the money logic lives in this module and the ledger.

IDENTITY CHURN. The ledger allocates a fresh row id on every insert -- you
cannot recreate a deleted transaction under its old id. So undo of a create
(delete) followed by redo (recreate) yields a DIFFERENT id, as does undo of a
delete. A stack entry recorded later may reference the old id. The manager keeps
a small remap table so ``resolve(old_id)`` always follows the chain to whatever
row currently stands in for it; every recreate records ``remap(old, new)``.

WHAT IS NOT UNDOABLE (deliberate, safe limitations). Turning a plain
transaction into a transfer (``convert_to_transfer``) or re-pointing a transfer
at a different account (``retarget_transfer``) add/remove a mirror leg and have
no clean ledger inverse; recording one as a field edit would be wrong. Those are
treated as a BARRIER -- the redo stack is dropped (a later redo could not safely
compose across them) while existing undo history is left intact. Batch (multi-
row) edits are likewise a barrier in this version: single-row add / edit /
delete / transfer / split are the undoable acts.
"""
from __future__ import annotations

from typing import Optional

from mammon import ledger

# Fields restored through ledger.update_transaction. Deliberately EXCLUDES
# id / account_id / created_at (identity, never rewritten) and
# transfer_account_id / transfer_pair_id (linkage owned solely by the transfer
# functions). ``tag`` takes the junction path and is handled separately -- it is
# per-leg and, unlike date/amount/payee/memo, never mirrored to a transfer's
# other side.
_RESTORE_FIELDS = ("date", "num", "payee", "category_id", "memo", "amount",
                   "cleared", "reconciled", "scheduled", "fitid", "import_id")


def _snapshot(conn, txn_id) -> Optional[dict]:
    """A full, replayable picture of one transaction: every writable field, its
    comma-joined tags, and its split lines. ``None`` when the row is gone."""
    row = ledger.get_transaction(conn, txn_id)
    if row is None:
        return None
    snap = {k: row[k] for k in _RESTORE_FIELDS}
    snap["id"] = int(row["id"])
    snap["account_id"] = int(row["account_id"])
    snap["transfer_account_id"] = row["transfer_account_id"]
    snap["transfer_pair_id"] = row["transfer_pair_id"]
    snap["tag"] = ledger.tags_text(conn, txn_id)
    snap["splits"] = ledger.get_splits(conn, txn_id)
    return snap


def _splits_key(splits) -> tuple:
    """Comparable shape of a transaction's split lines (order-sensitive), so two
    snapshots can be tested for a real change without spurious diffs on the
    display-only keys get_splits also returns (id, category_label)."""
    return tuple(
        (s["category_id"], s["transfer_account_id"], int(s["amount"]),
         s["memo"] or "", s["tag_id"])
        for s in splits
    )


def _structural(before: dict, after: dict) -> bool:
    """True when the edit changed a transaction's transfer LINKAGE -- a convert
    (plain <-> transfer) or a retarget (transfer to a different account). These
    have no clean ledger inverse (see the module docstring)."""
    return (before["transfer_account_id"] != after["transfer_account_id"]
            or (before["transfer_pair_id"] is None)
            != (after["transfer_pair_id"] is None))


def _differs(before: dict, after: dict) -> bool:
    """True when a user-visible field, the tags, or the split lines changed."""
    for k in _RESTORE_FIELDS:
        if before[k] != after[k]:
            return True
    if before["tag"] != after["tag"]:
        return True
    return _splits_key(before["splits"]) != _splits_key(after["splits"])


def _restore(mgr: "UndoManager", orig_id: int, snap: dict) -> None:
    """Put the transaction identified (through the remap) by ``orig_id`` back to
    the state in ``snap``. Restores fields + tags first, then splits: amount is
    set before set_splits so the split lines rebalance against the right total."""
    conn = mgr.conn
    tid = mgr.resolve(orig_id)
    if ledger.get_transaction(conn, tid) is None:
        return
    fields = {k: snap[k] for k in _RESTORE_FIELDS}
    fields["tag"] = snap["tag"]
    ledger.update_transaction(conn, tid, **fields)
    if snap["splits"]:
        ledger.set_splits(conn, tid, snap["splits"])
    elif ledger.has_splits(conn, tid):
        ledger.clear_splits(conn, tid)


def _restore_leg_extras(conn, leg_id: int, snap: dict) -> None:
    """After create_transfer rebuilds a transfer, re-apply the PER-LEG fields it
    could not distinguish between the two sides -- num, Clr/Reconciled status and
    tags. date/amount/payee/memo are intentionally left alone: they are already
    correct on both legs (create_transfer wrote them, the mirror keeps them in
    sync), and rewriting them here would bounce back onto the other leg."""
    ledger.update_transaction(
        conn, leg_id,
        num=snap["num"], cleared=snap["cleared"], reconciled=snap["reconciled"],
        scheduled=snap["scheduled"], fitid=snap["fitid"],
        import_id=snap["import_id"], tag=snap["tag"],
    )


def _delete(mgr: "UndoManager", ids) -> None:
    """Delete the (remapped) rows. delete_transaction is a no-op on a missing
    row and deletes both sides of a transfer, so deleting a transfer's two legs
    in turn is safe: the second call sees the row already gone."""
    for i in ids:
        ledger.delete_transaction(mgr.conn, mgr.resolve(i))


def _recreate(mgr: "UndoManager", snaps) -> None:
    """Recreate the transaction(s) from ``snaps`` through the ledger, recording a
    remap from each original id to its freshly allocated one. A two-leg transfer
    snapshot goes back through create_transfer so both mirror sides return."""
    conn = mgr.conn
    if len(snaps) == 2 and all(s["transfer_account_id"] is not None for s in snaps):
        a, b = snaps
        frm = a if int(a["amount"]) < 0 else b
        to = b if frm is a else a
        from_id, to_id = ledger.create_transfer(
            conn, frm["account_id"], to["account_id"], frm["date"],
            abs(int(frm["amount"])), memo=frm["memo"], num=frm["num"],
            cleared=frm["cleared"], payee=frm["payee"], reconciled=frm["reconciled"],
        )
        _restore_leg_extras(conn, from_id, frm)
        _restore_leg_extras(conn, to_id, to)
        mgr.remap(mgr.resolve(frm["id"]), from_id)
        mgr.remap(mgr.resolve(to["id"]), to_id)
        return
    s = snaps[0]
    new_id = ledger.add_transaction(
        conn, s["account_id"], s["date"], int(s["amount"]),
        num=s["num"], payee=s["payee"], category_id=s["category_id"],
        memo=s["memo"], cleared=s["cleared"], reconciled=s["reconciled"],
        scheduled=s["scheduled"], fitid=s["fitid"], import_id=s["import_id"],
        tag=s["tag"],
    )
    if s["splits"]:
        ledger.set_splits(conn, new_id, s["splits"])
    mgr.remap(mgr.resolve(s["id"]), new_id)


# --------------------------------------------------------------------------
# Actions: each knows how to undo() and redo() itself through the ledger.
# --------------------------------------------------------------------------
class _Add:
    """A transaction (or transfer) the user created. Undo deletes it; redo
    recreates it. ``snaps`` are captured just AFTER creation."""

    def __init__(self, snaps, transfer):
        self.snaps = snaps
        self.transfer = transfer
        self.ids = [s["id"] for s in snaps]

    @property
    def label(self) -> str:
        return "Add transfer" if self.transfer else "Add transaction"

    def undo(self, mgr):
        _delete(mgr, self.ids)

    def redo(self, mgr):
        _recreate(mgr, self.snaps)


class _Delete:
    """A transaction (or transfer) the user deleted. Undo recreates it; redo
    deletes it again. ``snaps`` are captured just BEFORE deletion."""

    def __init__(self, snaps, transfer):
        self.snaps = snaps
        self.transfer = transfer
        self.ids = [s["id"] for s in snaps]

    @property
    def label(self) -> str:
        return "Delete transfer" if self.transfer else "Delete transaction"

    def undo(self, mgr):
        _recreate(mgr, self.snaps)

    def redo(self, mgr):
        _delete(mgr, self.ids)


class _Edit:
    """A field / split change on an existing transaction. Its id is stable
    across the edit, so no remap happens here -- only a resolve at replay time."""

    def __init__(self, txn_id, before, after, label="Edit transaction"):
        self.txn_id = txn_id
        self.before = before
        self.after = after
        self._label = label

    @property
    def label(self) -> str:
        return self._label

    def undo(self, mgr):
        _restore(mgr, self.txn_id, self.before)

    def redo(self, mgr):
        _restore(mgr, self.txn_id, self.after)


class UndoManager:
    """A per-register, session-scoped undo/redo stack. Records inverse ops and
    replays them through the ledger; holds no Qt and writes no SQL directly."""

    def __init__(self, conn):
        self.conn = conn
        self._undo: list = []
        self._redo: list = []
        self._remap: dict[int, int] = {}

    # -- queries the Edit menu reads ---------------------------------------
    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> Optional[str]:
        return self._undo[-1].label if self._undo else None

    def redo_label(self) -> Optional[str]:
        return self._redo[-1].label if self._redo else None

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._remap.clear()

    # -- id remapping across recreate --------------------------------------
    def resolve(self, txn_id: int) -> int:
        seen: set[int] = set()
        while txn_id in self._remap and txn_id not in seen:
            seen.add(txn_id)
            txn_id = self._remap[txn_id]
        return txn_id

    def remap(self, old: int, new: int) -> None:
        if old != new:
            self._remap[old] = int(new)

    # -- recording (called by the model's write chokepoints) ---------------
    def capture(self, txn_id) -> Optional[dict]:
        """Snapshot a transaction for a later before/after edit record."""
        return _snapshot(self.conn, txn_id)

    def capture_many(self, txn_ids) -> list:
        """Snapshot several transactions (a transfer's two legs) BEFORE a delete;
        drops any that are already gone."""
        out = []
        for i in txn_ids:
            snap = _snapshot(self.conn, i)
            if snap is not None:
                out.append(snap)
        return out

    def record_add(self, txn_ids, *, transfer=False) -> None:
        snaps = self.capture_many(txn_ids)
        if snaps:
            self._push(_Add(snaps, transfer))

    def push_delete(self, snaps, *, transfer=False) -> None:
        """Record a delete whose snapshots were captured before it ran."""
        if snaps:
            self._push(_Delete(snaps, transfer))

    def record_edit(self, txn_id, before, *, label="Edit transaction") -> None:
        after = _snapshot(self.conn, txn_id)
        if before is None or after is None:
            return
        if _structural(before, after):
            self.barrier()
            return
        if not _differs(before, after):
            return  # a write that changed nothing is not an undo step
        self._push(_Edit(txn_id, before, after, label))

    def barrier(self) -> None:
        """An un-invertible or batch write happened: drop the redo stack so a
        later redo cannot replay across it. Existing undo history is kept."""
        self._redo.clear()

    def _push(self, action) -> None:
        self._undo.append(action)
        self._redo.clear()

    # -- execution ---------------------------------------------------------
    def undo(self) -> bool:
        if not self._undo:
            return False
        action = self._undo.pop()
        action.undo(self)
        self._redo.append(action)
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        action = self._redo.pop()
        action.redo(self)
        self._undo.append(action)
        return True
