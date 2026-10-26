"""Synthetic-only checks for isolated parallel account stores and safe merge."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tiktok_live.account_store import load_account_store, save_account_store_atomic
from tiktok_live.parallel_state import IsolatedAccountRuns


def account(account_id, uid, **extra):
    return {"id": account_id, "cookies": [
        {"name": "uid_tt", "value": uid, "domain": ".tiktok.com", "path": "/"},
    ], "username": "before", **extra}


class IsolatedAccountRunsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "accounts.json"
        self.temporary_root = self.root / "tasks"
        self.a = account("first", "synthetic-first")
        self.b = account("second", "synthetic-second")
        self.save([self.a, self.b])

    def save(self, accounts):
        save_account_store_atomic(self.path, {"version": 1, "accounts": accounts})

    def runs(self, selected=None):
        return IsolatedAccountRuns(self.path, self.temporary_root, selected)

    def update(self, path, **fields):
        store = load_account_store(path)
        store["accounts"][0].update(fields)
        save_account_store_atomic(path, store)

    def remove(self, path):
        store = load_account_store(path)
        store["accounts"] = []
        save_account_store_atomic(path, store)

    def test_room_stores_are_distinct_selected_only_and_seeded_before_tasks(self):
        with self.runs(("first",)) as runs:
            self.assertEqual([item["id"] for item in runs.accounts], ["first"])
            first = runs.create_store("../../room", "first")
            second = runs.create_store("other/room", "first")
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_relative_to(self.temporary_root.resolve()))
            left, right = load_account_store(first), load_account_store(second)
            self.assertEqual([item["id"] for item in left["accounts"]], ["first"])
            self.assertEqual(left["accounts"], right["accounts"])
            self.assertEqual(left["accounts"][0]["environment_seed"], "65d836c85d805997")
            real = load_account_store(self.path)
            self.assertEqual(left["accounts"][0], real["accounts"][0])
            self.assertRegex(real["accounts"][1]["environment_seed"], r"^[a-f0-9]{16}$")
            with self.assertRaises(ValueError):
                runs.create_store("room", "second")
            task_parent = first.parent
        self.assertFalse(task_parent.exists())
        self.assertTrue(self.path.exists())

    def test_normalization_retains_first_id_and_maps_selected_duplicate_alias(self):
        self.save([self.a, account("alias", "synthetic-first", updated_at="2026-01-01"), self.b])
        with self.runs(("alias", "first", "alias")) as runs:
            self.assertEqual([item["id"] for item in runs.accounts], ["first"])
            self.assertEqual([item["id"] for item in load_account_store(self.path)["accounts"]],
                             ["first", "second"])
            seed = runs.accounts[0]["environment_seed"]
        with self.runs() as runs:
            self.assertEqual(runs.accounts[0]["environment_seed"], seed)

    def test_conflicting_uid_with_shared_session_is_not_mapped_to_other_account(self):
        shared = {"name": "sessionid", "value": "shared", "domain": ".tiktok.com", "path": "/"}
        self.a["cookies"].append(shared)
        self.b["cookies"].append(deepcopy(shared))
        self.save([self.a, self.b])
        with self.runs(("second",)) as runs:
            self.assertEqual([item["id"] for item in runs.accounts], ["second"])

    def conflicting_groups(self, *, alias_last=False):
        shared = {"name": "sessionid", "value": "shared", "domain": ".tiktok.com", "path": "/"}
        first = account("first", "uid-one", updated_at="2026-01-01")
        first["cookies"].append(deepcopy(shared))
        alias = {"id": "alias", "cookies": [deepcopy(shared)], "updated_at": "2026-02-01"}
        selected = account("selected", "uid-two")
        selected["cookies"].append(deepcopy(shared))
        return [first, selected, alias] if alias_last else [first, alias, selected]

    def test_newer_session_only_alias_cannot_expand_conflicting_uid_selection(self):
        self.save(self.conflicting_groups())
        with self.runs(("selected",)) as runs:
            self.assertEqual([item["id"] for item in runs.accounts], ["selected"])
            with self.assertRaises(ValueError):
                runs.create_store("room", "first")

    def test_selected_session_alias_maps_only_to_its_actual_conflicting_group(self):
        self.save(self.conflicting_groups())
        with self.runs(("alias",)) as runs:
            self.assertEqual([item["id"] for item in runs.accounts], ["first"])
            with self.assertRaises(ValueError):
                runs.create_store("room", "selected")

    def test_selected_session_alias_after_conflicting_groups_stays_separate(self):
        self.save(self.conflicting_groups(alias_last=True))
        with self.runs(("alias",)) as runs:
            self.assertEqual([item["id"] for item in runs.accounts], ["alias"])
            for account_id in ("first", "selected"):
                with self.subTest(account_id=account_id), self.assertRaises(ValueError):
                    runs.create_store("room", account_id)

    def test_empty_or_unknown_selection_fails_without_starting_tasks(self):
        before = self.path.read_bytes()
        for selected in ((), ("unknown",)):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                with self.runs(selected):
                    self.fail("invalid selection entered")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(self.temporary_root.exists())

    def test_ambiguous_multi_uid_selection_does_not_select_shared_cookie_account(self):
        ambiguous = account("ambiguous", "synthetic-first")
        ambiguous["cookies"].append({
            "name": "multi_sids", "value": "11111:a,22222:b",
            "domain": ".tiktok.com", "path": "/",
        })
        self.save([self.a, ambiguous])
        with self.runs(("ambiguous",)) as runs:
            self.assertEqual([item["id"] for item in runs.accounts], ["ambiguous"])

    def test_initial_normalization_aborts_if_external_update_wins_staging_race(self):
        def interleaved_save(path, store, **kwargs):
            external = load_account_store(path)
            external["accounts"][0]["username"] = "external update"
            save_account_store_atomic(path, external)
            save_account_store_atomic(path, store, **kwargs)

        with patch("tiktok_live.parallel_state.save_account_store_atomic", side_effect=interleaved_save):
            with self.assertRaises(RuntimeError):
                with self.runs():
                    self.fail("stale normalization entered")
        self.assertEqual(load_account_store(self.path)["accounts"][0]["username"], "external update")
        self.assertFalse(self.temporary_root.exists())

    def test_exit_after_exception_preserves_unrelated_temp_files_and_does_not_commit(self):
        self.temporary_root.mkdir()
        sentinel = self.temporary_root / "unrelated.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            with self.runs() as runs:
                task = runs.create_store("room", "first")
                self.update(task, username="uncommitted")
                runs.collect_outcome("first", task, {})
                raise RuntimeError("synthetic failure")
        self.assertFalse(task.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(load_account_store(self.path)["accounts"][0]["username"], "before")

    def test_commit_does_not_overwrite_external_change_during_atomic_staging(self):
        with self.runs() as runs:
            task = runs.create_store("room", "first")
            self.update(task, username="task refresh")
            runs.collect_outcome("first", task, {})

            def interleaved_save(path, store, **kwargs):
                external = load_account_store(path)
                external["accounts"][0]["username"] = "external update"
                save_account_store_atomic(path, external)
                save_account_store_atomic(path, store, **kwargs)

            with patch("tiktok_live.parallel_state.save_account_store_atomic", side_effect=interleaved_save):
                runs.commit()
        self.assertEqual(load_account_store(self.path)["accounts"][0]["username"], "external update")

    def test_public_snapshot_mutation_does_not_change_task_or_merge_baseline(self):
        with self.runs(("first",)) as runs:
            runs.accounts[0]["username"] = "public mutation"
            task = runs.create_store("room", "first")
            self.assertEqual(load_account_store(task)["accounts"][0]["username"], "before")
            self.update(task, username="refreshed")
            runs.collect_outcome("first", task, {})
            runs.commit()
        self.assertEqual(load_account_store(self.path)["accounts"][0]["username"], "refreshed")

    def test_last_changed_completion_wins_and_unchanged_later_result_does_not_mask_it(self):
        with self.runs() as runs:
            first = runs.create_store("one", "first")
            last = runs.create_store("two", "first")
            unchanged = runs.create_store("three", "first")
            self.update(first, username="earlier")
            self.update(last, username="later")
            runs.collect_outcome("first", first, {})
            runs.collect_outcome("first", last, {})
            runs.collect_outcome("first", unchanged, {})
            self.update(last, username="mutated after collect")
            runs.commit()
        self.assertEqual(load_account_store(self.path)["accounts"][0]["username"], "later")

    def test_absence_without_positive_expiry_counter_never_deletes(self):
        for counters in ({}, {"expired": 0}, {"expired": -1}, {"expired": "1"}):
            with self.subTest(counters=counters), self.runs() as runs:
                task = runs.create_store("room", "first")
                self.remove(task)
                runs.collect_outcome("first", task, counters)
                runs.commit()
                self.assertEqual(len(load_account_store(self.path)["accounts"]), 2)

    def test_expiry_counter_without_removal_never_deletes(self):
        with self.runs() as runs:
            task = runs.create_store("room", "first")
            runs.collect_outcome("first", task, {"expired": 1})
            runs.commit()
        self.assertEqual(len(load_account_store(self.path)["accounts"]), 2)

    def test_confirmed_expiry_wins_over_refresh_in_either_collection_order(self):
        for expiry_first in (True, False):
            self.save([self.a, self.b])
            with self.subTest(expiry_first=expiry_first), self.runs() as runs:
                expired = runs.create_store("one", "first")
                refreshed = runs.create_store("two", "first")
                self.remove(expired)
                self.update(refreshed, username="refreshed")
                outcomes = [(expired, {"expired": 1}), (refreshed, {})]
                for task, counters in outcomes if expiry_first else reversed(outcomes):
                    runs.collect_outcome("first", task, counters)
                runs.commit()
            self.assertEqual([item["id"] for item in load_account_store(self.path)["accounts"]],
                             ["second"])

    def test_runner_exception_has_no_outcome_and_never_deletes(self):
        with self.runs() as runs:
            task = runs.create_store("room", "first")
            try:
                self.remove(task)
                raise RuntimeError("synthetic runner exception")
            except RuntimeError:
                pass  # Coordinator must not call collect_outcome on this path.
            runs.commit()
        self.assertEqual(len(load_account_store(self.path)["accounts"]), 2)

    def test_external_record_changes_block_refresh_and_expiry_but_preserve_other_merges(self):
        for expire in (False, True):
            self.save([self.a, self.b])
            with self.subTest(expire=expire), self.runs() as runs:
                first = runs.create_store("one", "first")
                second = runs.create_store("one", "second")
                self.remove(first) if expire else self.update(first, username="task refresh")
                self.update(second, username="second refresh")
                runs.collect_outcome("first", first, {"expired": int(expire)})
                runs.collect_outcome("second", second, {})
                store = load_account_store(self.path)
                store["accounts"][0]["external_custom_field"] = {"preserve": True}
                store["accounts"].append(account("added", "synthetic-added"))
                save_account_store_atomic(self.path, store)
                runs.commit()
            real = load_account_store(self.path)["accounts"]
            self.assertEqual(real[0]["username"], "before")
            self.assertEqual(real[0]["external_custom_field"], {"preserve": True})
            self.assertEqual(real[1]["username"], "second refresh")
            self.assertEqual(real[2]["id"], "added")

    def test_external_removal_is_not_undone(self):
        with self.runs() as runs:
            task = runs.create_store("room", "first")
            self.update(task, username="task refresh")
            runs.collect_outcome("first", task, {})
            self.save([load_account_store(self.path)["accounts"][1]])
            runs.commit()
        self.assertEqual([item["id"] for item in load_account_store(self.path)["accounts"]], ["second"])

    def test_corrupt_missing_blank_and_read_failure_do_not_infer_expiry(self):
        for failure in ("corrupt", "missing", "blank", "unreadable"):
            with self.subTest(failure=failure), self.runs() as runs:
                task = runs.create_store("room", "first")
                self.remove(task)
                if failure == "corrupt":
                    task.write_text("{invalid", encoding="utf-8")
                elif failure == "missing":
                    task.unlink()
                elif failure == "blank":
                    task.write_text("   ", encoding="utf-8")
                if failure == "unreadable":
                    with patch("tiktok_live.parallel_state.load_account_store", side_effect=OSError):
                        runs.collect_outcome("first", task, {"expired": 1})
                else:
                    runs.collect_outcome("first", task, {"expired": 1})
                runs.commit()
            self.assertEqual(len(load_account_store(self.path)["accounts"]), 2)

    def test_unknown_path_wrong_account_and_foreign_provenance_are_ignored(self):
        with self.runs() as runs:
            first = runs.create_store("one", "first")
            second = runs.create_store("two", "second")
            foreign = self.root / "foreign.json"
            save_account_store_atomic(foreign, {"version": 1, "accounts": []})
            runs.collect_outcome("first", foreign, {"expired": 1})
            self.remove(second)
            runs.collect_outcome("first", second, {"expired": 1})
            first.write_bytes(second.read_bytes())
            runs.collect_outcome("first", first, {"expired": 1})
            runs.commit()
        self.assertEqual(len(load_account_store(self.path)["accounts"]), 2)

    def test_wrong_identity_extra_record_and_changed_id_are_rejected(self):
        for mutation in ("identity", "extra", "id"):
            with self.subTest(mutation=mutation), self.runs() as runs:
                task = runs.create_store("room", "first")
                store = load_account_store(task)
                if mutation == "identity":
                    store["accounts"][0]["cookies"] = deepcopy(self.b["cookies"])
                elif mutation == "extra":
                    store["accounts"].append(self.b)
                else:
                    store["accounts"][0]["id"] = "second"
                store["accounts"][0]["username"] = "wrong"
                save_account_store_atomic(task, store)
                runs.collect_outcome("first", task, {"expired": 1})
                runs.commit()
            self.assertEqual(load_account_store(self.path)["accounts"][0]["username"], "before")

    def test_unchanged_outcome_and_empty_outcome_do_not_rewrite_formal_store(self):
        with self.runs() as runs:
            before = self.path.stat().st_mtime_ns
            task = runs.create_store("room", "first")
            runs.collect_outcome("first", task, {})
            runs.commit()
            self.assertEqual(self.path.stat().st_mtime_ns, before)
            runs.commit()
            self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_independent_contexts_and_parallel_collectors_do_not_share_task_state(self):
        with self.runs() as left, self.runs() as right:
            one = left.create_store("room", "first")
            two = right.create_store("room", "first")
            self.assertNotEqual(one, two)
            tasks = [left.create_store(str(index), "first") for index in range(20)]
            for index, task in enumerate(tasks):
                self.update(task, username=f"refreshed-{index}")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda task: left.collect_outcome("first", task, {}), tasks))
            left.commit()
            self.assertTrue(load_account_store(self.path)["accounts"][0]["username"].startswith("refreshed-"))
            self.assertTrue(two.exists())


if __name__ == "__main__":
    unittest.main()
