"""Completed forget watermarks: temporary SQLite state, no model or services."""
import contextlib
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from laowu.registry import Registry
from laowu.runtime import initialize_runtime
from laowu.scheduling import ScheduleBook
from laowu.store import Store


class ForgetRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="laowu-forget-restore-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.make_store("live")

    def make_store(self, name):
        store = Store(self.root / name)
        initialize_runtime(store)
        Registry(store).ensure(dict(id="engineer", name="test", description="synthetic",
                                    instructions="no external operations", tools=[]), write_files=False)
        ScheduleBook(store)
        return store

    def work(self, store, name="fresh"):
        with store.transaction() as db:
            db.execute("""INSERT INTO tasks
                (id,session_id,friend_id,client,request,workspace,status,checkpoint,result,
                 request_hash,created_at,updated_at)
                VALUES(?,'closed','engineer','codex',?,?,'completed',?,?,'hash','2026-01-01','2026-01-01')""",
                       (name, name + " request", str(store.root),
                        json.dumps({"summary": name + " checkpoint", "memory_ids": []}),
                        json.dumps({"summary": name + " result", "artifacts": []})))
        ScheduleBook(store).upsert(dict(id="schedule_" + name, name=name,
                                       friend_id="engineer", request=name + " scheduled request",
                                       workspace=str(store.root), trigger={"type": "interval", "seconds": 86400}))

    def assert_work(self, store, name="fresh", *, retained=True):
        with contextlib.closing(store.connect()) as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (name,)).fetchone()
        self.assertIsNotNone(task)
        if retained:
            self.assertEqual(task["request"], name + " request")
            self.assertEqual(json.loads(task["checkpoint"])["summary"], name + " checkpoint")
            self.assertEqual(json.loads(task["result"])["summary"], name + " result")
            schedules = ScheduleBook(store).list()
            self.assertIn("schedule_" + name, [s["id"] for s in schedules])
            self.assertTrue(next(s for s in schedules if s["id"] == "schedule_" + name)["enabled"])
        else:
            self.assertEqual(task["request"], "")
            self.assertEqual(task["checkpoint"], "{}")
            self.assertIsNone(task["result"])
            self.assertNotIn("schedule_" + name, [s["id"] for s in ScheduleBook(store).list()])

    def snapshot(self, store, name):
        destination = self.root / (name + ".zip")
        shutil.copyfile(store.backup()["path"], destination)
        return destination

    def rewrite_snapshot(self, path, name, change):
        with zipfile.ZipFile(path) as archive:
            blobs = {n: archive.read(n) for n in archive.namelist() if n != "manifest.json"}
            manifest = json.loads(archive.read("manifest.json"))
        change(manifest, blobs)
        manifest["files"] = {n: hashlib.sha256(data).hexdigest() for n, data in blobs.items()}
        target = self.root / (name + ".zip")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for n, data in blobs.items():
                archive.writestr(n, data)
        return target

    def forget(self, store, content="old fact"):
        fact = store.memory_upsert(content)
        store.memory_forget(fact["id"])
        return fact

    def test_new_snapshot_preserves_post_forget_work_on_repeated_restore(self):
        self.forget(self.store)
        self.work(self.store)
        snapshot = self.snapshot(self.store, "new")
        for _ in range(2):
            self.store.restore(snapshot)
            self.assert_work(self.store)
            self.assertFalse(self.store._ledger()["pending"])

    def test_new_backup_upgrades_completed_live_legacy_ledger(self):
        self.forget(self.store)
        self.work(self.store)
        ledger = self.store._ledger()
        ledger.pop("cleanup_events")
        self.store.ledger_path.write_text(json.dumps(ledger))
        with self.store.transaction() as db:
            db.execute("DELETE FROM memory_meta WHERE key='forget_applied_watermark'")
        snapshot = self.snapshot(self.store, "upgraded_live")
        with zipfile.ZipFile(snapshot) as archive:
            self.assertIn("forget_watermark", json.loads(archive.read("manifest.json")))
        self.store.restore(snapshot)
        self.assert_work(self.store)

    def test_pre_forget_snapshot_cannot_revive_memory_or_task_material(self):
        fact = self.store.memory_upsert("PRIVATE OLD FACT")
        self.work(self.store, "private_material")
        snapshot = self.snapshot(self.store, "before_forget")
        self.store.memory_forget(fact["id"])
        self.store.restore(snapshot)
        self.assertEqual(self.store.memory_search(), [])
        self.assert_work(self.store, "private_material", retained=False)
        self.assertIn(fact["id"], self.store._ledger()["ids"])

    def test_legacy_snapshot_without_memory_rows_still_scrubs_isolated_material(self):
        self.forget(self.store)
        self.work(self.store, "orphaned_private_material")
        snapshot = self.snapshot(self.store, "source")
        legacy = self.rewrite_snapshot(snapshot, "legacy", lambda m, b: m.pop("forget_watermark"))
        target = self.make_store("legacy_target")
        target.restore(legacy)
        self.assert_work(target, "orphaned_private_material", retained=False)
        self.work(target, "after_legacy_cleanup")
        upgraded = self.snapshot(target, "after_legacy_cleanup")
        target.restore(upgraded)
        self.assert_work(target, "after_legacy_cleanup")

    def test_newer_same_lineage_snapshot_covers_older_target(self):
        self.forget(self.store, "first forgotten fact")
        older = self.snapshot(self.store, "older")
        target = self.make_store("older_target")
        target.restore(older)
        self.forget(self.store, "second forgotten fact")
        self.work(self.store, "newer_work")
        newer = self.snapshot(self.store, "newer")
        target.restore(newer)
        self.assert_work(target, "newer_work")
        self.assertEqual(set(target._ledger()["cleanup_events"]), set(self.store._ledger()["cleanup_events"]))

    def test_repeated_forget_same_id_requires_new_cleanup_event(self):
        fact = self.forget(self.store)
        self.work(self.store, "between_forgets")
        older = self.snapshot(self.store, "between_forgets")
        first = set(self.store._ledger()["cleanup_events"])
        self.store.memory_forget(fact["id"])
        self.assertEqual(len(set(self.store._ledger()["cleanup_events"]) - first), 1)
        self.store.restore(older)
        self.assert_work(self.store, "between_forgets", retained=False)
        self.assertEqual(len(self.store._ledger()["cleanup_events"]), 2)

    def test_independent_deletion_branches_merge_and_later_backup_covers_both(self):
        a = self.store.memory_upsert("branch A fact")
        b = self.store.memory_upsert("branch B fact")
        initial = self.snapshot(self.store, "branches_initial")
        other = self.make_store("other_branch")
        other.restore(initial)
        self.store.memory_forget(a["id"])
        other.memory_forget(b["id"])
        self.work(other, "branch_b_work")
        branch_b = self.snapshot(other, "branch_b")
        self.store.restore(branch_b)
        self.assertEqual(self.store.memory_search(), [])
        self.assert_work(self.store, "branch_b_work", retained=False)
        self.assertEqual(set(self.store._ledger()["ids"]), {a["id"], b["id"]})
        self.assertEqual(len(self.store._ledger()["cleanup_events"]), 2)
        self.work(self.store, "merged_work")
        merged = self.snapshot(self.store, "merged")
        other.restore(merged)
        self.assert_work(other, "merged_work")
        self.assertEqual(other.memory_search(), [])

    def test_pending_import_does_not_receive_completed_snapshot_proof(self):
        self.forget(self.store)
        self.work(self.store, "uncertain_private_material")
        self.store._write_ledger(set(self.store._ledger()["ids"]), pending=True)
        snapshot = self.snapshot(self.store, "pending_import")
        with zipfile.ZipFile(snapshot) as archive:
            self.assertNotIn("forget_watermark", json.loads(archive.read("manifest.json")))
        target = self.make_store("pending_import_target")
        target.restore(snapshot)
        self.assert_work(target, "uncertain_private_material", retained=False)
        self.assertFalse(target._ledger()["pending"])

    def test_pending_source_or_target_overrides_matching_watermark(self):
        self.forget(self.store)
        self.work(self.store, "pending_material")
        snapshot = self.snapshot(self.store, "matching_proof")

        def mark_pending(manifest, blobs):
            ledger = json.loads(blobs["forgotten.json"])
            ledger["pending"] = True
            blobs["forgotten.json"] = json.dumps(ledger).encode()

        pending = self.rewrite_snapshot(snapshot, "pending_matching_proof", mark_pending)
        target = self.make_store("pending_proof_target")
        target.restore(pending)
        self.assert_work(target, "pending_material", retained=False)
        self.store._write_ledger(set(self.store._ledger()["ids"]), pending=True)
        self.store.restore(snapshot)
        self.assert_work(self.store, "pending_material", retained=False)

    def test_rolled_back_pending_forget_remains_required_during_restore(self):
        fact = self.store.memory_upsert("PRIVATE ROLLBACK FACT")
        self.work(self.store, "rollback_material")
        old = self.snapshot(self.store, "rollback_old")
        with patch.object(self.store, "_clear_runtime_files", side_effect=OSError("synthetic file failure")):
            with self.assertRaises(OSError):
                self.store.memory_forget(fact["id"])
        self.assertTrue(self.store._ledger()["pending"])
        self.store.restore(old)
        self.assertEqual(self.store.memory_search(), [])
        self.assert_work(self.store, "rollback_material", retained=False)
        self.assertFalse(self.store._ledger()["pending"])

    def test_pending_restore_file_cleanup_replay_preserves_committed_new_work(self):
        self.forget(self.store)
        self.work(self.store)
        snapshot = self.snapshot(self.store, "cleanup_pending")
        with patch.object(self.store, "_clear_runtime_files", side_effect=OSError("synthetic restore failure")):
            with self.assertRaises(OSError):
                self.store.restore(snapshot)
        self.assertTrue(self.store._ledger()["pending"])
        restarted = Store(self.store.root)
        self.assertFalse(restarted._ledger()["pending"])
        self.assert_work(restarted)

    def test_invalid_or_mismatched_proof_cannot_bypass_cleanup(self):
        self.forget(self.store)
        self.work(self.store, "unproven_material")
        snapshot = self.snapshot(self.store, "unproven")
        invalid = self.rewrite_snapshot(snapshot, "wrong_proof", lambda m, b: m.update(forget_watermark="wrong"))
        target = self.make_store("wrong_proof_target")
        target.restore(invalid)
        self.assert_work(target, "unproven_material", retained=False)

    def test_derived_invalidation_preserves_pending_cleanup_and_events(self):
        self.forget(self.store)
        source = self.store.memory_upsert("source before correction")
        self.store.memory_upsert("derived", derived_from=[source["id"]])
        ledger = self.store._ledger()
        self.store._write_ledger(set(ledger["ids"]), pending=True)
        self.store.memory_upsert("source after correction", memory_id=source["id"])
        updated = self.store._ledger()
        self.assertTrue(updated["pending"])
        self.assertEqual(updated["cleanup_events"], ledger["cleanup_events"])

    def test_resurfaced_rows_cannot_receive_proof_and_are_scrubbed(self):
        fact = self.forget(self.store)
        self.work(self.store, "resurfaced_material")
        with self.store.transaction() as db:
            db.execute("""INSERT INTO memory_records
                VALUES(?,?,'fact','personal','user','explicit','laowu',1,'old','old','{}')""",
                       (fact["id"], "PRIVATE RESURFACED FACT"))
        snapshot = self.snapshot(self.store, "resurfaced")
        with zipfile.ZipFile(snapshot) as archive:
            self.assertNotIn("forget_watermark", json.loads(archive.read("manifest.json")))
        target = self.make_store("resurfaced_target")
        target.restore(snapshot)
        self.assertEqual(target.memory_search(), [])
        self.assert_work(target, "resurfaced_material", retained=False)

    def test_earlier_forget_cannot_finalize_later_failed_intent(self):
        a = self.store.memory_upsert("first fact")
        b = self.store.memory_upsert("second fact")
        committed, proceed = threading.Event(), threading.Event()
        original_lock = self.store._lock
        acquisitions = 0
        failures = []

        @contextlib.contextmanager
        def interleaved_lock(timeout=None):
            nonlocal acquisitions
            if threading.current_thread().name == "first-forget":
                acquisitions += 1
                if acquisitions == 2:
                    committed.set()
                    if not proceed.wait(5):
                        raise TimeoutError("test interleave timed out")
            with original_lock(timeout):
                yield

        def first_forget():
            try:
                self.store.memory_forget(a["id"])
            except BaseException as exc:
                failures.append(exc)

        with patch.object(self.store, "_lock", side_effect=interleaved_lock):
            first = threading.Thread(target=first_forget, name="first-forget", daemon=True)
            first.start()
            try:
                self.assertTrue(committed.wait(5))
                self.work(self.store, "later_private_material")
                with patch.object(self.store, "_clear_runtime_files", side_effect=OSError("second cleanup failed")):
                    with self.assertRaises(OSError):
                        self.store.memory_forget(b["id"])
            finally:
                proceed.set()
                first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(self.store._ledger()["pending"])
        restarted = Store(self.store.root)
        self.assertFalse(restarted._ledger()["pending"])
        self.assertEqual(restarted.memory_search(), [])
        self.assert_work(restarted, "later_private_material", retained=False)

    def test_unconfirmed_live_group_blocks_forget_and_restore_without_erasing_witness(self):
        fact = self.store.memory_upsert("memory")
        snapshot = self.snapshot(self.store, "before_pending_group")
        witness = self.store.private / "runtime" / "orphan" / "group.json"
        witness.parent.mkdir(parents=True)
        witness.write_text('{"synthetic":"unconfirmed group"}')
        with self.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS run_cleanup(run_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO run_cleanup VALUES('orphan')")
        for action in (lambda: self.store.memory_forget(fact["id"]), lambda: self.store.restore(snapshot)):
            with self.assertRaisesRegex(RuntimeError, "未确认清理"):
                action()
            self.assertTrue(witness.is_file())
        self.assertFalse(self.store._ledger()["pending"])
        self.assertEqual(len(self.store.memory_search()), 1)

    def test_imported_group_cleanup_leases_do_not_cross_restore(self):
        with self.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS run_cleanup(run_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO run_cleanup VALUES('source_machine_group')")
        snapshot = self.snapshot(self.store, "source_cleanup_lease")
        target = self.make_store("cleanup_lease_target")
        target.restore(snapshot)
        with contextlib.closing(target.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM run_cleanup").fetchone()[0], 0)

    def test_startup_resurfaced_descendants_persist_intent_before_failed_cleanup(self):
        fact = self.store.memory_upsert("resurfaced parent")
        child = self.store.memory_upsert("resurfaced child", derived_from=[fact["id"]])
        self.work(self.store, "resurfaced_startup_material")
        self.store._write_ledger({fact["id"]}, pending=False)
        with patch.object(Store, "_clear_runtime_files", side_effect=OSError("startup cleanup failed")):
            with self.assertRaises(OSError):
                Store(self.store.root)
        ledger = self.store._ledger()
        self.assertTrue(ledger["pending"])
        self.assertEqual(set(ledger["ids"]), {fact["id"], child["id"]})
        restarted = Store(self.store.root)
        self.assertFalse(restarted._ledger()["pending"])
        self.assertEqual(restarted.memory_search(), [])
        self.assert_work(restarted, "resurfaced_startup_material", retained=False)

    def test_malformed_cleanup_events_fail_closed_before_restore_changes_live_state(self):
        self.work(self.store)
        snapshot = self.snapshot(self.store, "valid_ledger")

        def corrupt(manifest, blobs):
            ledger = json.loads(blobs["forgotten.json"])
            ledger["cleanup_events"] = ["not-a-valid-cleanup-event"]
            blobs["forgotten.json"] = json.dumps(ledger).encode()

        bad = self.rewrite_snapshot(snapshot, "malformed_ledger", corrupt)
        with self.assertRaisesRegex(ValueError, "遗忘记录损坏"):
            self.store.restore(bad)
        self.assert_work(self.store)

    def test_unknown_versions_and_malformed_pending_or_events_fail_closed(self):
        for field, value in (("version", 2), ("version", True), ("version", "1"),
                             ("pending", "false"), ("pending", 0),
                             ("cleanup_events", "forget_" + "0" * 32),
                             ("cleanup_events", [1]), ("cleanup_events", ["invalid"])):
            with self.subTest(field=field, value=value):
                ledger = {"ids": [], "pending": False, field: value}
                with self.assertRaisesRegex(ValueError, "遗忘记录损坏"):
                    Store._validated_ledger(ledger)
        self.assertEqual(Store._validated_ledger({"ids": []})["cleanup_events"], [])


if __name__ == "__main__":
    unittest.main()
