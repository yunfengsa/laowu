"""ScheduleBook contracts; all databases/workspaces are temporary."""
import concurrent.futures
import contextlib
import copy
import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from laowu.registry import Registry
from laowu.scheduling import BATCH_SIZE, ScheduleBook
from laowu.store import Store

UTC = timezone.utc
BASE = datetime(2026, 10, 30, tzinfo=UTC)


def _tick_process(root, at):
    """Supervised subprocess exercising real SQLite/file locks, no daemon."""
    return ScheduleBook(Store(root)).due(at)


class ScheduleBookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "老五 项目"
        self.workspace = self.root / "中文 工作目录"
        self.workspace.mkdir(parents=True)
        self.store = Store(self.root)
        self.registry = Registry(self.store)
        self.registry.ensure({"id": "engineer", "name": "阿衡", "description": "测试工程师",
                              "instructions": "限定工作目录", "tools": ["read"]}, write_files=False)
        self.clock = patch("laowu.scheduling._now", return_value=BASE)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.book = ScheduleBook(self.store)

    def spec(self, **changes):
        spec = {"id": "stable_schedule", "name": "中文定时任务", "friend_id": "engineer",
                "request": "检查中文路径中的进度", "workspace": str(self.workspace),
                "trigger": {"type": "interval", "seconds": 60}}
        spec.update(changes)
        return spec

    def date_spec(self, at, **changes):
        return self.spec(trigger={"type": "date", "run_at": at.isoformat()}, **changes)

    def raw(self, sql, parameters=()):
        with contextlib.closing(self.store.connect()) as db:
            return [dict(row) for row in db.execute(sql, parameters)]

    def test_date_chinese_paths_and_required_occurrence_fields(self):
        local = datetime(2026, 10, 30, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
        schedule = self.book.upsert(self.date_spec(local, context="root"))
        self.assertEqual(schedule["timezone"], "Asia/Shanghai")
        self.assertEqual(schedule["misfire_grace_seconds"], 86400)
        self.assertEqual(schedule["workspace"], str(self.workspace))
        self.assertEqual(datetime.fromisoformat(schedule["next_run_at"]), local)
        self.assertEqual(self.book.due(local - timedelta(microseconds=1)), [])
        occurrence, = self.book.due(local)
        self.assertNotEqual(occurrence["id"], schedule["id"])
        self.assertEqual(occurrence["schedule_id"], schedule["id"])
        self.assertEqual(occurrence["friend_id"], "engineer")
        self.assertEqual(occurrence["request"], "检查中文路径中的进度")
        self.assertEqual(occurrence["workspace"], str(self.workspace))
        self.assertEqual(occurrence["context"], "root")
        self.assertEqual(datetime.fromisoformat(occurrence["scheduled_at"]), local)
        self.assertTrue(occurrence["scheduled_at"].endswith("+00:00"))
        self.assertIsNone(occurrence["task_id"])
        self.assertEqual(self.book.due(local), [])
        self.assertIsNone(self.book.list()[0]["next_run_at"])

    def test_naive_date_is_local_and_z_date_works_on_python310(self):
        local = self.spec(trigger={"type": "date", "run_at": "2026-10-30T08:02:00"})
        first = self.book.upsert(local)
        equivalent = self.spec(trigger={"type": "date", "run_at": "2026-10-30T00:02:00Z"})
        self.assertEqual(self.book.upsert(equivalent), first)
        self.assertEqual(len(self.book.due(BASE + timedelta(minutes=2))), 1)

    def test_past_dates_catch_up_once_with_inclusive_grace(self):
        for seconds, expected in ((30, 1), (31, 0)):
            with self.subTest(seconds=seconds):
                self.book.upsert(self.date_spec(BASE - timedelta(seconds=seconds),
                                               id=f"past_{seconds}", misfire_grace_seconds=30))
                self.assertEqual(len(self.book.due(BASE)), expected)
                self.assertEqual(self.book.due(BASE + timedelta(seconds=1)), [])
        self.assertTrue(all(row["next_run_at"] is None for row in self.book.list()))
        self.assertEqual(len(self.book.pending()), 1)

    def test_interval_first_run_and_microsecond_boundary(self):
        self.clock_mock.return_value = BASE + timedelta(microseconds=123456)
        first = self.clock_mock.return_value + timedelta(seconds=10.5)
        schedule = self.book.upsert(self.spec(trigger={"type": "interval", "seconds": 10.5}))
        self.assertEqual(datetime.fromisoformat(schedule["next_run_at"]), first)
        self.assertEqual(self.book.due(first - timedelta(microseconds=1)), [])
        for i in range(4):
            at = first + timedelta(seconds=10.5 * i)
            occurrence, = self.book.due(at)
            self.assertEqual(datetime.fromisoformat(occurrence["scheduled_at"]), at)
            self.assertEqual(self.book.due(at), [])

    def test_interval_misfire_coalesces_latest_without_replaying_backlog(self):
        self.book.upsert(self.spec())
        at = BASE + timedelta(days=3650, seconds=37)
        occurrence, = self.book.due(at)
        self.assertEqual(datetime.fromisoformat(occurrence["scheduled_at"]), at - timedelta(seconds=37))
        self.assertEqual(len(self.book.pending()), 1)
        self.assertEqual(self.book.due(at), [])
        self.assertGreater(datetime.fromisoformat(self.book.list()[0]["next_run_at"]), at)

    def test_expired_recurring_occurrence_skips_and_cursor_advances(self):
        self.book.upsert(self.spec(misfire_grace_seconds=5))
        at = BASE + timedelta(seconds=179)
        self.assertEqual(self.book.due(at), [])
        self.assertEqual(self.book.pending(), [])
        self.assertEqual(datetime.fromisoformat(self.book.list()[0]["next_run_at"]), BASE + timedelta(seconds=180))
        occurrence, = self.book.due(BASE + timedelta(seconds=180))
        self.assertEqual(datetime.fromisoformat(occurrence["scheduled_at"]), BASE + timedelta(seconds=180))

    def test_cron_uses_local_zone_and_skips_old_occurrences(self):
        schedule = self.book.upsert(self.spec(trigger={"type": "cron", "expression": "15 9 * * *"}))
        self.assertEqual(datetime.fromisoformat(schedule["next_run_at"]), BASE + timedelta(hours=1, minutes=15))
        at = BASE + timedelta(days=120, hours=2)
        occurrence, = self.book.due(at)
        self.assertEqual(datetime.fromisoformat(occurrence["scheduled_at"]), at.replace(hour=1, minute=15))
        self.assertEqual(self.book.due(at), [])
        self.assertEqual(datetime.fromisoformat(self.book.list()[0]["next_run_at"]),
                         at.replace(hour=1, minute=15) + timedelta(days=1))

    def test_standard_cron_weekday_numbers_names_ranges_and_steps(self):
        expressions = (("0 9 * * 0", 2), ("0 9 * * 7", 2), ("0 9 * * sun", 2),
                       ("0 9 * * 1", 3), ("0 9 * * mon-fri", 0),
                       ("*/15 9 * * 1-5", 0), ("0 9 * oct fri", 0))
        for i, (expression, days) in enumerate(expressions):
            with self.subTest(expression=expression):
                schedule = self.book.upsert(self.spec(id=f"cron_{i}",
                    trigger={"type": "cron", "expression": expression}))
                self.assertEqual(datetime.fromisoformat(schedule["next_run_at"]),
                                 BASE + timedelta(days=days, hours=1))

    def test_standard_cron_restricted_day_fields_are_or_and_deduplicate(self):
        self.clock_mock.return_value = datetime(2026, 11, 1, tzinfo=UTC)
        schedule = self.book.upsert(self.spec(timezone="UTC",
            trigger={"type": "cron", "expression": "0 9 15 * 1"}))
        self.assertEqual(datetime.fromisoformat(schedule["next_run_at"]), datetime(2026, 11, 2, 9, tzinfo=UTC))
        occurrence, = self.book.due(datetime(2026, 11, 15, 9, tzinfo=UTC))
        self.assertEqual(datetime.fromisoformat(occurrence["scheduled_at"]), datetime(2026, 11, 15, 9, tzinfo=UTC))
        # June 15 2026 is both the 15th and Monday: still exactly one occurrence.
        self.clock_mock.return_value = datetime(2026, 6, 14, tzinfo=UTC)
        self.book.upsert(self.spec(id="both", timezone="UTC",
            trigger={"type": "cron", "expression": "0 9 15 * 1"}))
        at = datetime(2026, 6, 15, 9, tzinfo=UTC)
        self.assertEqual(len(self.book.due(at)), 1)
        self.assertEqual(self.book.due(at), [])

    def test_cron_dst_fold_preserves_distinct_utc_instants(self):
        self.clock_mock.return_value = datetime(2026, 10, 31, 12, tzinfo=UTC)
        self.book.upsert(self.spec(timezone="America/New_York",
            trigger={"type": "cron", "expression": "30 1 * * *"}))
        first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        occurrence1, = self.book.due(first)
        occurrence2, = self.book.due(second)
        self.assertEqual(datetime.fromisoformat(occurrence1["scheduled_at"]), first)
        self.assertEqual(datetime.fromisoformat(occurrence2["scheduled_at"]), second)
        self.assertNotEqual(occurrence1["id"], occurrence2["id"])

    def test_restart_recovers_pending_and_attach_is_idempotent(self):
        self.book.upsert(self.date_spec(BASE))
        occurrence, = self.book.due()
        restarted = ScheduleBook(Store(self.root))
        self.assertEqual(restarted.pending(), [occurrence])
        self.assertEqual(restarted.due(BASE), [])
        attached = restarted.attach_task(occurrence["id"], "task_saved")
        self.assertEqual(attached["task_id"], "task_saved")
        self.assertEqual(restarted.attach_task(occurrence["id"], "task_saved"), attached)
        self.assertEqual(ScheduleBook(Store(self.root)).pending(), [])
        with self.assertRaises(ValueError):
            restarted.attach_task(occurrence["id"], "task_different")
        self.assertEqual(self.raw("SELECT task_id FROM schedule_occurrences")[0]["task_id"], "task_saved")

    def test_same_id_same_config_does_not_reset_next_run_or_revision(self):
        spec = self.spec()
        schedule = self.book.upsert(spec)
        self.book.due(BASE + timedelta(seconds=60))
        after_tick = self.book.list()[0]
        self.clock_mock.return_value = BASE + timedelta(days=3)
        restarted = ScheduleBook(Store(self.root))
        self.assertEqual(restarted.upsert(copy.deepcopy(spec)), after_tick)
        self.assertEqual(restarted.list()[0]["revision"], schedule["revision"])

    def test_equivalent_integer_float_intervals_are_idempotent(self):
        first = self.book.upsert(self.spec())
        self.clock_mock.return_value = BASE + timedelta(seconds=15)
        self.assertEqual(self.book.upsert(self.spec(trigger={"type": "interval", "seconds": 60.0})), first)

    def test_update_revision_preserves_cursor_and_old_pending_snapshot(self):
        spec = self.spec()
        self.book.upsert(spec)
        first, = self.book.due(BASE + timedelta(seconds=60))
        next_run = self.book.list()[0]["next_run_at"]
        spec["request"] = "改为执行新请求"
        spec["context"] = "root"
        updated = self.book.upsert(spec)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["next_run_at"], next_run)
        self.assertEqual(self.book.pending(), [first])
        second, = self.book.due(BASE + timedelta(seconds=120))
        self.assertEqual(second["request"], spec["request"])
        self.assertEqual(second["context"], "root")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(first["request"], "检查中文路径中的进度")

    def test_trigger_edit_resets_cursor(self):
        self.book.upsert(self.spec())
        self.clock_mock.return_value = BASE + timedelta(seconds=20)
        edited = self.book.upsert(self.spec(trigger={"type": "interval", "seconds": 90}))
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(datetime.fromisoformat(edited["next_run_at"]), BASE + timedelta(seconds=110))
        self.assertEqual(self.book.due(BASE + timedelta(seconds=60)), [])

    def test_consumed_date_is_not_revived_by_retries_edits_or_enable(self):
        spec = self.date_spec(BASE)
        self.book.upsert(spec)
        self.book.due(BASE)
        self.assertIsNone(self.book.upsert(spec)["next_run_at"])
        spec["name"] = "只改名称"
        self.assertIsNone(self.book.upsert(spec)["next_run_at"])
        self.book.set_enabled(spec["id"], False)
        self.assertIsNone(self.book.set_enabled(spec["id"], True)["next_run_at"])
        self.assertEqual(self.book.due(BASE + timedelta(seconds=1)), [])

    def test_disabled_schedule_reenable_coalesces_and_noop_is_stable(self):
        spec = self.spec(enabled=False)
        schedule = self.book.upsert(spec)
        self.assertEqual(self.book.due(BASE + timedelta(seconds=120)), [])
        self.assertEqual(self.book.set_enabled(schedule["id"], False), schedule)
        enabled = self.book.set_enabled(schedule["id"], True)
        self.assertEqual(enabled["revision"], 2)
        occurrence, = self.book.due(BASE + timedelta(seconds=185))
        self.assertEqual(datetime.fromisoformat(occurrence["scheduled_at"]), BASE + timedelta(seconds=180))
        self.assertEqual(occurrence["revision"], 2)
        self.assertEqual(self.book.set_enabled(schedule["id"], True)["revision"], 2)
        self.book.set_enabled(schedule["id"], False)
        self.assertEqual(self.book.due(BASE + timedelta(seconds=240)), [])

    def test_soft_delete_retains_history_and_pending_but_cannot_revive(self):
        spec = self.spec()
        self.book.upsert(spec)
        first, = self.book.due(BASE + timedelta(seconds=60))
        self.book.attach_task(first["id"], "task_completed")
        pending, = self.book.due(BASE + timedelta(seconds=120))
        deleted = self.book.delete(spec["id"])
        self.assertFalse(deleted["enabled"])
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(self.book.list(), [])
        self.assertEqual(self.book.delete(spec["id"]), deleted)
        self.assertEqual(self.book.due(BASE + timedelta(days=1)), [])
        restarted = ScheduleBook(Store(self.root))
        self.assertEqual(restarted.pending(), [pending])
        self.assertEqual(len(self.raw("SELECT * FROM schedule_occurrences")), 2)
        with self.assertRaises(ValueError):
            restarted.upsert(spec)
        with self.assertRaises(ValueError):
            restarted.set_enabled(spec["id"], True)

    def test_generated_ids_and_relative_workspace(self):
        spec = self.spec(workspace="中文 工作目录")
        del spec["id"]
        first = self.book.upsert(spec)
        second = self.book.upsert(spec)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["workspace"], str(self.workspace))

    def test_invalid_configuration_never_persists(self):
        bad = [None, [], {}, self.spec(id="../bad"), self.spec(id=None),
               self.spec(name=" "), self.spec(request=3), self.spec(friend_id="missing"),
               self.spec(workspace=str(self.workspace / "不存在")), self.spec(workspace="\0"),
               self.spec(timezone="Not/AZone"), self.spec(timezone="/etc/passwd"),
               self.spec(enabled=1), self.spec(context="sudo"), self.spec(context={}),
               self.spec(misfire_grace_seconds=-1), self.spec(misfire_grace_seconds=True),
               self.spec(misfire_grace_seconds=1.5), self.spec(extra="ignored?"),
               self.spec(trigger=None), self.spec(trigger={"type": "unknown"}),
               self.spec(trigger={"type": "interval", "seconds": 9.9}),
               self.spec(trigger={"type": "interval", "seconds": True}),
               self.spec(trigger={"type": "interval", "seconds": "60"}),
               self.spec(trigger={"type": "interval", "seconds": float("nan")}),
               self.spec(trigger={"type": "interval", "seconds": float("inf")}),
               self.spec(trigger={"type": "interval", "seconds": 10 ** 200}),
               self.spec(trigger={"type": "interval", "seconds": 60, "jitter": 3}),
               self.spec(trigger={"type": "date", "run_at": "nonsense"}),
               self.spec(trigger={"type": "date", "run_at": 123}),
               self.spec(timezone="America/New_York", trigger={"type": "date", "run_at": "2026-03-08T02:30:00"}),
               self.spec(timezone="America/New_York", trigger={"type": "date", "run_at": "2026-11-01T01:30:00"})]
        for expression in ("* * * *", "0 * * * * *", "61 * * * *", "0 25 * * *", "0 0 0 * *",
                           "0 0 * 13 *", "0 0 * * 8", "*/0 * * * *", "*/-2 * * * *",
                           "0 0 1--2 * *", "0 0 * * fri-mon", "@daily", "0 0 last * *"):
            bad.append(self.spec(trigger={"type": "cron", "expression": expression}))
        for spec in bad:
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    self.book.upsert(spec)
                self.assertEqual(self.book.list(), [])

    def test_registry_requires_an_actual_active_friend(self):
        # A directory on disk is not a Registry entry.
        (self.root / "friends" / "imaginary").mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.book.upsert(self.spec(friend_id="imaginary"))
        with self.store.transaction() as db:
            db.execute("UPDATE friend_registry SET active=0 WHERE id='engineer'")
        with self.assertRaises(ValueError):
            self.book.upsert(self.spec())

    def test_secrets_in_any_stored_text_are_rejected(self):
        secret = "sk-" + "A1" * 20
        for field in ("id", "name", "request", "workspace", "friend_id", "timezone"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.book.upsert(self.spec(**{field: secret}))
        for request in ("api_key=" + "abc123" * 8, "ghp_" + "a" * 36, "-----BEGIN PRIVATE KEY-----"):
            with self.assertRaises(ValueError):
                self.book.upsert(self.spec(request=request))
        self.assertEqual(self.book.list(), [])
        self.book.upsert(self.spec(request="密钥应放在环境变量，不要写入请求。"))
        self.assertNotIn(secret, json.dumps(self.raw("SELECT * FROM schedules")))

    def test_invalid_method_inputs_and_naive_tick(self):
        for at in (BASE.replace(tzinfo=None), "2026-10-30", False):
            with self.assertRaises(ValueError):
                self.book.due(at)
        with self.assertRaises(ValueError):
            self.book.set_enabled("missing", 1)
        with self.assertRaises(ValueError):
            self.book.set_enabled("missing", True)
        with self.assertRaises(ValueError):
            self.book.delete("missing")
        with self.assertRaises(ValueError):
            self.book.attach_task("missing", "task_one")
        self.book.upsert(self.date_spec(BASE))
        occurrence, = self.book.due(BASE)
        with self.assertRaises(ValueError):
            self.book.attach_task(occurrence["id"], "")
        with self.assertRaises(ValueError):
            self.book.attach_task(occurrence["id"], "sk-" + "A1" * 20)

    def test_occurrence_and_cursor_rollback_atomically_on_cursor_failure(self):
        schedule = self.book.upsert(self.date_spec(BASE))
        with self.store.transaction() as db:
            db.execute("""CREATE TRIGGER fail_cursor BEFORE UPDATE OF next_run_at ON schedules
                BEGIN SELECT RAISE(ABORT, 'simulated cursor disk failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated cursor"):
            self.book.due(BASE)
        self.assertEqual(self.book.pending(), [])
        self.assertEqual(self.book.list(), [schedule])
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER fail_cursor")
        restarted = ScheduleBook(Store(self.root))
        self.assertEqual(len(restarted.due(BASE)), 1)
        self.assertEqual(len(restarted.pending()), 1)

    def test_batch_rolls_back_earlier_occurrences_if_later_insert_fails(self):
        for ident in ("a", "b"):
            self.book.upsert(self.date_spec(BASE, id=ident))
        before = self.book.list()
        with self.store.transaction() as db:
            db.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON schedule_occurrences
                WHEN NEW.schedule_id='b' BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated insert"):
            self.book.due(BASE)
        self.assertEqual(self.book.pending(), [])
        self.assertEqual(self.book.list(), before)
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER fail_second")
        self.assertEqual(len(self.book.due(BASE)), 2)

    def test_unique_key_also_prevents_replay_if_cursor_is_rewound(self):
        schedule = self.book.upsert(self.date_spec(BASE))
        occurrence, = self.book.due(BASE)
        with self.store.transaction() as db:
            db.execute("UPDATE schedules SET next_run_at=? WHERE id=?",
                       (schedule["next_run_at"], schedule["id"]))
        self.assertEqual(self.book.due(BASE), [])
        self.assertEqual(self.book.pending(), [occurrence])
        self.assertIsNone(self.book.list()[0]["next_run_at"])

    def test_concurrent_processes_produce_one_occurrence(self):
        self.book.upsert(self.date_spec(BASE))
        with concurrent.futures.ProcessPoolExecutor(max_workers=3,
                mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = [pool.submit(_tick_process, str(self.root), BASE) for _ in range(8)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(len(result) for result in results), 1)
        self.assertEqual(len(self.book.pending()), 1)

    def test_due_and_pending_are_bounded_and_eventually_drain(self):
        with patch("laowu.scheduling.BATCH_SIZE", 2):
            for i in range(5):
                self.book.upsert(self.date_spec(BASE, id=f"bounded_{i}"))
            self.assertEqual(len(self.book.due(BASE)), 2)
            self.assertEqual(len(self.book.due(BASE)), 2)
            self.assertEqual(len(self.book.due(BASE)), 1)
            self.assertEqual(self.book.due(BASE), [])
            count = 0
            while pending := self.book.pending():
                self.assertLessEqual(len(pending), 2)
                for occurrence in pending:
                    self.book.attach_task(occurrence["id"], f"task_{count}")
                    count += 1
            self.assertEqual(count, 5)
        self.assertGreater(BATCH_SIZE, 0)

    def test_initialization_is_idempotent_and_does_not_change_memory_schema(self):
        with contextlib.closing(self.store.connect()) as db:
            old_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0]
        self.book.upsert(self.spec())
        before = self.book.list()
        ScheduleBook(self.store)
        self.assertEqual(self.book.list(), before)
        with contextlib.closing(self.store.connect()) as db:
            new_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(old_tables, new_tables)
            self.assertEqual(db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0], version)

    def test_incompatible_existing_table_is_preserved_without_partial_schema(self):
        other = Store(self.root / "已有表")
        with other.transaction() as db:
            db.execute("CREATE TABLE schedules(id TEXT PRIMARY KEY, old_payload TEXT)")
            db.execute("INSERT INTO schedules VALUES('original','保留数据')")
        with self.assertRaisesRegex(ValueError, "结构不兼容"):
            ScheduleBook(other)
        with contextlib.closing(other.connect()) as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM schedules").fetchone()), ("original", "保留数据"))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='schedule_occurrences'").fetchone())

    def test_missing_occurrence_unique_key_is_rejected_without_changes(self):
        other = Store(self.root / "缺少唯一键")
        with other.transaction() as db:
            db.execute("""CREATE TABLE schedule_occurrences(id TEXT PRIMARY KEY,
                schedule_id TEXT, revision INTEGER, scheduled_at TEXT, spec_json TEXT,
                task_id TEXT, created_at TEXT)""")
        with self.assertRaisesRegex(ValueError, "缺少触发唯一键"):
            ScheduleBook(other)
        with contextlib.closing(other.connect()) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='schedules'").fetchone())


if __name__ == "__main__":
    unittest.main()
