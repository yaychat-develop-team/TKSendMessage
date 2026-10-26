"""Per-task saved accounts and conservative, explicit end-of-run merging."""

import hashlib
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .account_store import (
    _account_identity,
    _deduplicate_accounts,
    _ensure_environment_seeds,
    _identities_match,
    _identity_is_ambiguous,
    load_account_store,
    save_account_store_atomic,
)


_PROVENANCE_KEY = "_isolated_account_task"


class _StoreChanged(Exception):
    """The formal store changed while an atomic replacement was staged."""


class IsolatedAccountRuns:
    """Own task files without allowing runner threads to write the real store.

    Call ``collect_outcome`` only after a runner returns normally (including a
    cancelled return), never after it raises. Call ``commit`` once all runners
    have finished. Exiting cleans this context's files and does not auto-commit.
    """

    def __init__(self, store_path: Path, temporary_root: Path,
                 selected_account_ids: tuple[str, ...] | None):
        self._store_path = Path(store_path)
        self._temporary_root = Path(temporary_root)
        self._selected_ids = selected_account_ids
        self._lock = threading.Lock()
        self._temporary = None
        self._baseline = {}
        self._tasks = {}
        self._updates = {}
        self._expired = set()
        self.accounts: tuple[dict, ...] = ()

    def __enter__(self):
        if self._temporary is not None:
            raise RuntimeError("Account runs context is already active")
        store = load_account_store(self._store_path)
        original_store = deepcopy(store)
        selected_ids = None
        if self._selected_ids is not None:
            ids = set(self._selected_ids)
            canonical_ids = self._canonical_ids(store["accounts"])
            selected_ids = {canonical_ids[account_id] for account_id in ids
                            if account_id in canonical_ids}
            if not selected_ids:
                raise ValueError("No selected saved accounts")
        changed = _deduplicate_accounts(store)
        changed = _ensure_environment_seeds(store) or changed
        accounts = store["accounts"]
        if selected_ids is not None:
            accounts = [item for item in accounts if item["id"] in selected_ids]
        if changed:
            def guard():
                if load_account_store(self._store_path) != original_store:
                    raise RuntimeError("Saved accounts changed before task setup")

            save_account_store_atomic(self._store_path, store, before_replace=guard)
        self._baseline = {item["id"]: deepcopy(item) for item in accounts}
        self.accounts = tuple(deepcopy(accounts))
        self._tasks.clear()
        self._updates.clear()
        self._expired.clear()
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(
            prefix="account-runs-", dir=self._temporary_root,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with self._lock:
            temporary, self._temporary = self._temporary, None
            self._tasks.clear()
            self._updates.clear()
            self._expired.clear()
        if temporary is not None:
            temporary.cleanup()

    @staticmethod
    def _canonical_ids(accounts: list[dict]) -> dict[str, str]:
        """Track membership with the deduplicator's accumulated identities.

        Cookie refresh may erase a retained record's UID. Selection must use
        original group membership, including conflicting identities, rather
        than match selected records against those refreshed cookies.
        """
        groups = []
        for account in accounts:
            identity = _account_identity(account["cookies"])
            matches = [group for group in groups if _identities_match(group[2], identity)]
            combined = identity | set().union(*(group[2] for group in matches))
            if matches and not _identity_is_ambiguous(combined):
                group = matches[0]
                group[1].add(account["id"])
                group[2].update(identity)
                for other in matches[1:]:
                    group[1].update(other[1])
                    group[2].update(other[2])
                    groups.remove(other)
            else:
                groups.append((account["id"], {account["id"]}, identity))
        return {member: canonical for canonical, members, _ in groups for member in members}

    @staticmethod
    def _same_identity(original: dict, candidate: dict) -> bool:
        left = _account_identity(original["cookies"])
        right = _account_identity(candidate["cookies"])
        return (not _identity_is_ambiguous(left | right)
                and _identities_match(left, right))

    def _require_active(self):
        if self._temporary is None:
            raise RuntimeError("Account runs context is not active")

    def create_store(self, room: str, account_id: str) -> Path:
        """Create a registered, one-account file below our private temp root."""
        with self._lock:
            self._require_active()
            if account_id not in self._baseline:
                raise ValueError("Account is not selected")
            digest = hashlib.sha256(
                (str(room) + "\0" + account_id).encode("utf-8")
            ).hexdigest()
            token = uuid4().hex
            directory = Path(self._temporary.name) / (digest + "-" + token)
            directory.mkdir()
            path = (directory / "accounts.json").resolve()
            save_account_store_atomic(path, {
                "version": 1,
                "accounts": [deepcopy(self._baseline[account_id])],
                _PROVENANCE_KEY: token,
            })
            self._tasks[path] = (account_id, token)
            return path

    def collect_outcome(self, account_id: str, task_store: Path, counters: dict) -> None:
        """Snapshot valid task state; invalid files can never imply expiration."""
        with self._lock:
            self._require_active()
            try:
                path = Path(task_store).resolve()
                registration = self._tasks.get(path)
                if registration is None or registration[0] != account_id:
                    return
                store = load_account_store(path)
            except (OSError, ValueError):
                return
            if store.get(_PROVENANCE_KEY) != registration[1]:
                return
            accounts = store["accounts"]
            if not accounts:
                expired = counters.get("expired", 0)
                if type(expired) in (int, float) and expired > 0:
                    self._expired.add(account_id)
                return
            if len(accounts) != 1 or accounts[0]["id"] != account_id:
                return
            candidate = accounts[0]
            original = self._baseline[account_id]
            if (candidate["cookies"] != original["cookies"]
                    and not self._same_identity(original, candidate)):
                return
            if candidate != original:
                self._updates[account_id] = deepcopy(candidate)

    def commit(self) -> None:
        """Merge only unchanged formal records, preserving any external edits."""
        with self._lock:
            self._require_active()
            if not self._updates and not self._expired:
                return
            current = load_account_store(self._store_path)
            merged = deepcopy(current)
            accounts = []
            for record in merged["accounts"]:
                account_id = record["id"]
                if record == self._baseline.get(account_id):
                    if account_id in self._expired:
                        continue
                    if account_id in self._updates:
                        record = deepcopy(self._updates[account_id])
                accounts.append(record)
            merged["accounts"] = accounts
            if merged == current:
                return

            def guard():
                if load_account_store(self._store_path) != current:
                    raise _StoreChanged()

            try:
                save_account_store_atomic(self._store_path, merged, before_replace=guard)
            except _StoreChanged:
                # Another writer won the staging race. Never replace its store.
                return
