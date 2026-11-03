"""JSON schedules and a durable outbox of occurrences, without an APS job store.

The daemon may call ``due()`` once a second, then dispatch ``pending()`` using
each occurrence ID as the task submission idempotency key before attach_task().
No scheduler, thread, CLI, or model is started here. Both queue production and
cursor advancement use Store.transaction(), so a crash cannot lose an enqueue.

Schema (owned here, independently of the memory schema version):
* schedules: id, spec_json, revision, enabled, anchor_at, next_run_at,
  deleted_at, created_at, updated_at. anchor_at fixes the interval's first run.
* schedule_occurrences: id, schedule_id, revision, scheduled_at, spec_json,
  task_id, created_at; UNIQUE(schedule_id, revision, scheduled_at).

All stored/returned timestamps are UTC ISO strings; naive date inputs are local
to the configured IANA zone. Cron uses standard five-field weekday numbering
(0/7 = Sunday) and day-of-month/day-of-week OR semantics. Recurrences coalesce
to the latest missed occurrence within grace, never replay the entire backlog.
Disabling preserves the cursor; re-enabling applies the same coalescing policy.
Changing trigger/timezone resets the cursor; other edits preserve it. Consumed
dates stay consumed. Deletion is permanent for that ID and retains the outbox,
including already produced pending entries. due() and pending() are bounded.
"""
from __future__ import annotations

import contextlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .common import new_id
from .registry import Registry
from .store import _SECRET

UTC = timezone.utc
BATCH_SIZE = 100
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_FIELDS = {"id", "name", "friend_id", "request", "workspace", "trigger",
           "timezone", "enabled", "misfire_grace_seconds", "context"}
_SCHEDULE_COLUMNS = {
    "id": "TEXT", "spec_json": "TEXT", "revision": "INTEGER",
    "enabled": "INTEGER", "anchor_at": "TEXT", "next_run_at": "TEXT",
    "deleted_at": "TEXT", "created_at": "TEXT", "updated_at": "TEXT",
}
_OCCURRENCE_COLUMNS = {
    "id": "TEXT", "schedule_id": "TEXT", "revision": "INTEGER",
    "scheduled_at": "TEXT", "spec_json": "TEXT", "task_id": "TEXT",
    "created_at": "TEXT",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _load_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _text(value, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 须为 1–{maximum} 字符的文字")
    if _SECRET.search(value):
        raise ValueError("定时配置不能保存私钥、密钥或访问令牌")
    return value


def _ident(value) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("ID 须为 1–128 位字母、数字、下划线或短横线")
    return _text(value, "ID", 128)


def _date(value: str, zone: ZoneInfo) -> datetime:
    value = _text(value, "run_at", 128).strip()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None:
            local = parsed.replace(tzinfo=zone)
            if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != parsed:
                raise ValueError("当地时间不存在，请指定带偏移量的 ISO 时间")
            if local.utcoffset() != parsed.replace(tzinfo=zone, fold=1).utcoffset():
                raise ValueError("当地时间有夏令时歧义，请指定带偏移量的 ISO 时间")
            parsed = local
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValueError("run_at 必须是有效 ISO 日期时间；夏令时歧义须写明偏移量") from exc


def _cron_values(field: str, minimum: int, maximum: int, names: tuple = ()) -> str:
    """Expand a small standard cron field; let APScheduler calculate dates."""
    mapping = {name: minimum + i for i, name in enumerate(names)}

    def number(token):
        if token in mapping:
            return mapping[token]
        if not re.fullmatch(r"[0-9]{1,2}", token):
            raise ValueError("cron 字段无效")
        result = int(token)
        if not minimum <= result <= maximum:
            raise ValueError("cron 数值越界")
        return result

    values = set()
    for part in field.split(","):
        pieces = part.split("/")
        if len(pieces) > 2:
            raise ValueError("cron 步长无效")
        step = 1
        if len(pieces) == 2:
            if not re.fullmatch(r"[0-9]{1,2}", pieces[1]) or int(pieces[1]) < 1:
                raise ValueError("cron 步长须为正整数")
            step = int(pieces[1])
        base = pieces[0]
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2:
                raise ValueError("cron 范围无效")
            start, end = map(number, bounds)
        else:
            start = number(base)
            end = maximum if len(pieces) == 2 else start
        if start > end:
            raise ValueError("cron 范围起点不能大于终点")
        values.update(range(start, end + 1, step))
    return ",".join(str(v) for v in sorted(values))


def _cron(expression: str, zone: ZoneInfo):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron 必须为标准五字段：分 时 日 月 星期")
    minute, hour, day, month, weekday = fields
    minute = _cron_values(minute, 0, 59)
    hour = _cron_values(hour, 0, 23)
    day_values = _cron_values(day, 1, 31)
    month = _cron_values(month, 1, 12, ("jan", "feb", "mar", "apr", "may", "jun",
                                        "jul", "aug", "sep", "oct", "nov", "dec"))
    # Traditional cron starts with Sunday; APScheduler 3.x starts with Monday.
    weekdays = _cron_values(weekday, 0, 7, ("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
    weekday_values = ",".join(str(v) for v in sorted({(int(v) + 6) % 7 for v in weekdays.split(",")}))
    common = {"minute": minute, "hour": hour, "month": month,
              "second": 0, "timezone": zone}
    if not day.startswith("*") and not weekday.startswith("*"):
        return OrTrigger([CronTrigger(day=day_values, **common),
                          CronTrigger(day_of_week=weekday_values, **common)])
    return CronTrigger(day=day_values, day_of_week=weekday_values, **common)


def _trigger(spec: dict, anchor: datetime):
    zone = ZoneInfo(spec["timezone"])
    trigger = spec["trigger"]
    if trigger["type"] == "date":
        return DateTrigger(run_date=_load_time(trigger["run_at"]), timezone=zone)
    if trigger["type"] == "interval":
        return IntervalTrigger(seconds=trigger["seconds"], start_date=anchor, timezone=zone)
    return _cron(trigger["expression"], zone)


def _latest(trigger, first: datetime, at: datetime) -> datetime:
    if isinstance(trigger, DateTrigger):
        return first
    if isinstance(trigger, IntervalTrigger):
        following = trigger.get_next_fire_time(None, at + timedelta(microseconds=1))
        return following.astimezone(UTC) - trigger.interval
    # Binary search the final matching second, using only APScheduler's forward
    # API. Work grows logarithmically with downtime, not with missed periods.
    low, high = int(first.timestamp()), int(at.timestamp())
    while low < high:
        middle = (low + high + 1) // 2
        candidate = trigger.get_next_fire_time(None, datetime.fromtimestamp(middle, UTC))
        if candidate is not None and candidate.astimezone(UTC) <= at:
            low = middle
        else:
            high = middle - 1
    return trigger.get_next_fire_time(None, datetime.fromtimestamp(low, UTC)).astimezone(UTC)


def _schema_check(db, table: str, columns: dict) -> None:
    existing = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
    if not existing:
        return
    # Names come solely from the two constants below, never caller input.
    actual = {r["name"]: r for r in db.execute(f"PRAGMA table_info({table})")}
    if (existing["type"] != "table" or not columns.keys() <= actual.keys()
            or any(actual[name]["type"].upper() != kind for name, kind in columns.items())
            or [name for name, row in actual.items() if row["pk"]] != ["id"]):
        raise ValueError(f"已有 {table} 结构不兼容；未覆盖，请先备份并显式迁移")
    if table == "schedule_occurrences":
        unique_keys = []
        for index in db.execute(f"PRAGMA index_list({table})"):
            if index["unique"] and not index["partial"]:
                escaped = index["name"].replace('"', '""')
                unique_keys.append([r["name"] for r in db.execute(f'PRAGMA index_info("{escaped}")')])
        if ["schedule_id", "revision", "scheduled_at"] not in unique_keys:
            raise ValueError("已有 schedule_occurrences 缺少触发唯一键；未覆盖")


class ScheduleBook:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            _schema_check(db, "schedules", _SCHEDULE_COLUMNS)
            _schema_check(db, "schedule_occurrences", _OCCURRENCE_COLUMNS)
            db.execute("""CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY NOT NULL, spec_json TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                anchor_at TEXT NOT NULL, next_run_at TEXT, deleted_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_occurrences (
                id TEXT PRIMARY KEY NOT NULL,
                schedule_id TEXT NOT NULL REFERENCES schedules(id),
                revision INTEGER NOT NULL, scheduled_at TEXT NOT NULL,
                spec_json TEXT NOT NULL, task_id TEXT, created_at TEXT NOT NULL,
                UNIQUE(schedule_id,revision,scheduled_at))""")
            db.execute("""CREATE INDEX IF NOT EXISTS schedules_due
                ON schedules(next_run_at,id) WHERE enabled=1 AND deleted_at IS NULL""")
            db.execute("""CREATE INDEX IF NOT EXISTS schedule_occurrences_pending
                ON schedule_occurrences(scheduled_at,id) WHERE task_id IS NULL""")
        self.registry = Registry(store)

    def _validate(self, spec: dict) -> dict:
        if not isinstance(spec, dict) or spec.keys() - _FIELDS:
            raise ValueError("定时配置必须为对象且不能含未知字段")
        result = {"id": _ident(spec["id"]) if "id" in spec else new_id("schedule")}
        for field, maximum in (("name", 512), ("friend_id", 64), ("request", 32000), ("workspace", 4096)):
            result[field] = _text(spec.get(field), field, maximum)
        self.registry.get(result["friend_id"])
        workspace = Path(result["workspace"]).expanduser()
        if not workspace.is_absolute():
            workspace = self.store.root / workspace
        try:
            workspace = workspace.resolve()
            if not workspace.is_dir():
                raise ValueError("工作目录不存在或不是目录")
        except (OSError, RuntimeError) as exc:
            raise ValueError("工作目录无效") from exc
        result["workspace"] = _text(str(workspace), "workspace", 4096)
        zone_name = _text(spec.get("timezone", "Asia/Shanghai"), "timezone", 128)
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone 必须是有效 IANA 时区") from exc
        result["timezone"] = zone_name
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须为布尔值")
        result["enabled"] = enabled
        grace = spec.get("misfire_grace_seconds", 86400)
        if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
            raise ValueError("misfire_grace_seconds 必须为非负整数")
        result["misfire_grace_seconds"] = grace
        context = spec.get("context", "normal")
        if context not in ("normal", "root"):
            raise ValueError("context 只能为 normal 或 root")
        result["context"] = context
        trigger = spec.get("trigger")
        if not isinstance(trigger, dict):
            raise ValueError("trigger 必须为对象")
        kind = trigger.get("type")
        if kind == "date" and trigger.keys() == {"type", "run_at"}:
            normalized = {"type": kind, "run_at": _stamp(_date(trigger["run_at"], zone))}
        elif kind == "interval" and trigger.keys() == {"type", "seconds"}:
            seconds = trigger["seconds"]
            try:
                if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                        or not math.isfinite(seconds) or seconds < 10):
                    raise ValueError("interval.seconds 必须为至少 10 秒的有限数值")
                _now() + timedelta(seconds=seconds)
            except OverflowError as exc:
                raise ValueError("interval.seconds 超出可用日期范围") from exc
            # JSON distinguishes 60 from 60.0; the schedule must not.
            seconds = int(seconds) if isinstance(seconds, float) and seconds.is_integer() else seconds
            normalized = {"type": kind, "seconds": seconds}
        elif kind == "cron" and trigger.keys() == {"type", "expression"}:
            expression = " ".join(_text(trigger["expression"], "cron", 512).lower().split())
            _cron(expression, zone)
            normalized = {"type": kind, "expression": expression}
        else:
            raise ValueError("trigger 只支持 date/run_at、interval/seconds、cron/expression")
        result["trigger"] = normalized
        return result

    @staticmethod
    def _schedule(row) -> dict:
        return {**json.loads(row["spec_json"]), **{key: row[key] for key in
                ("revision", "anchor_at", "next_run_at", "deleted_at", "created_at", "updated_at")}}

    @staticmethod
    def _occurrence(row) -> dict:
        return {**json.loads(row["spec_json"]), **{key: row[key] for key in
                ("id", "schedule_id", "revision", "scheduled_at", "task_id", "created_at")}}

    @staticmethod
    def _get(db, schedule_id, *, deleted=False):
        row = db.execute("SELECT * FROM schedules WHERE id=?", (_ident(schedule_id),)).fetchone()
        if row is None:
            raise ValueError("定时任务不存在")
        if row["deleted_at"] and not deleted:
            raise ValueError("定时任务已删除，原 ID 不允许重新启用或重建")
        return row

    def upsert(self, spec: dict) -> dict:
        config = self._validate(spec)
        encoded = json.dumps(config, ensure_ascii=False, sort_keys=True)
        at = _now()
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM schedules WHERE id=?", (config["id"],)).fetchone()
            if old and old["deleted_at"]:
                raise ValueError("定时任务已删除，原 ID 不允许重建")
            if old and old["spec_json"] == encoded:
                return self._schedule(old)
            previous = json.loads(old["spec_json"]) if old else None
            if previous and all(previous[key] == config[key] for key in ("trigger", "timezone")):
                anchor, next_run = old["anchor_at"], old["next_run_at"]
            else:
                start = at
                if config["trigger"]["type"] == "interval":
                    start += timedelta(seconds=config["trigger"]["seconds"])
                anchor = _stamp(start)
                first = _trigger(config, start).get_next_fire_time(None, at)
                next_run = _stamp(first) if first is not None else None
            revision = old["revision"] + 1 if old else 1
            timestamp = _stamp(at)
            db.execute("""INSERT INTO schedules
                (id,spec_json,revision,enabled,anchor_at,next_run_at,deleted_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,NULL,?,?)
                ON CONFLICT(id) DO UPDATE SET spec_json=excluded.spec_json,
                revision=excluded.revision, enabled=excluded.enabled,
                anchor_at=excluded.anchor_at, next_run_at=excluded.next_run_at,
                updated_at=excluded.updated_at""",
                (config["id"], encoded, revision, int(config["enabled"]), anchor, next_run,
                 old["created_at"] if old else timestamp, timestamp))
            return self._schedule(self._get(db, config["id"]))

    def list(self) -> list[dict]:
        with contextlib.closing(self.store.connect()) as db:
            return [self._schedule(row) for row in db.execute(
                "SELECT * FROM schedules WHERE deleted_at IS NULL ORDER BY created_at,id")]

    def set_enabled(self, schedule_id, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须为布尔值")
        with self.store.transaction() as db:
            row = self._get(db, schedule_id)
            if bool(row["enabled"]) != enabled:
                config = json.loads(row["spec_json"])
                config["enabled"] = enabled
                if enabled:
                    self._validate(config)
                db.execute("""UPDATE schedules SET enabled=?,spec_json=?,
                    revision=revision+1,updated_at=? WHERE id=?""",
                    (int(enabled), json.dumps(config, ensure_ascii=False, sort_keys=True),
                     _stamp(_now()), schedule_id))
            return self._schedule(self._get(db, schedule_id))

    def delete(self, schedule_id) -> dict:
        with self.store.transaction() as db:
            row = self._get(db, schedule_id, deleted=True)
            if row["deleted_at"] is None:
                config = json.loads(row["spec_json"])
                config["enabled"] = False
                timestamp = _stamp(_now())
                db.execute("""UPDATE schedules SET enabled=0,spec_json=?,next_run_at=NULL,
                    deleted_at=?,updated_at=?,revision=revision+1 WHERE id=?""",
                    (json.dumps(config, ensure_ascii=False, sort_keys=True), timestamp, timestamp, schedule_id))
            return self._schedule(self._get(db, schedule_id, deleted=True))

    def due(self, at: datetime | None = None) -> list[dict]:
        at = _now() if at is None else at
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at 必须是带时区的 datetime")
        at = at.astimezone(UTC)
        emitted = []
        with self.store.transaction() as db:
            rows = db.execute("""SELECT * FROM schedules WHERE enabled=1 AND deleted_at IS NULL
                AND next_run_at<=? ORDER BY next_run_at,id LIMIT ?""", (_stamp(at), BATCH_SIZE)).fetchall()
            for row in rows:
                config = json.loads(row["spec_json"])
                trigger = _trigger(config, _load_time(row["anchor_at"]))
                scheduled = _latest(trigger, _load_time(row["next_run_at"]), at)
                next_run = (None if isinstance(trigger, DateTrigger) else
                            trigger.get_next_fire_time(None, at + timedelta(microseconds=1)))
                if (at - scheduled).total_seconds() <= config["misfire_grace_seconds"]:
                    occurrence_id = new_id("occurrence")
                    inserted = db.execute("""INSERT INTO schedule_occurrences
                        (id,schedule_id,revision,scheduled_at,spec_json,task_id,created_at)
                        VALUES (?,?,?,?,?,NULL,?)
                        ON CONFLICT(schedule_id,revision,scheduled_at) DO NOTHING""",
                        (occurrence_id, row["id"], row["revision"], _stamp(scheduled),
                         row["spec_json"], _stamp(at)))
                    if inserted.rowcount:
                        emitted.append(self._occurrence(db.execute(
                            "SELECT * FROM schedule_occurrences WHERE id=?", (occurrence_id,)).fetchone()))
                db.execute("UPDATE schedules SET next_run_at=?,updated_at=? WHERE id=?",
                           (_stamp(next_run) if next_run is not None else None, _stamp(at), row["id"]))
        return emitted

    def pending(self) -> list[dict]:
        with contextlib.closing(self.store.connect()) as db:
            return [self._occurrence(row) for row in db.execute("""SELECT * FROM schedule_occurrences
                WHERE task_id IS NULL ORDER BY scheduled_at,id LIMIT ?""", (BATCH_SIZE,))]

    def attach_task(self, occurrence_id, task_id) -> dict:
        occurrence_id, task_id = _ident(occurrence_id), _ident(task_id)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM schedule_occurrences WHERE id=?", (occurrence_id,)).fetchone()
            if row is None:
                raise ValueError("触发记录不存在")
            if row["task_id"] is not None and row["task_id"] != task_id:
                raise ValueError("触发记录已绑定其他任务")
            db.execute("UPDATE schedule_occurrences SET task_id=? WHERE id=?", (task_id, occurrence_id))
            return self._occurrence(db.execute(
                "SELECT * FROM schedule_occurrences WHERE id=?", (occurrence_id,)).fetchone())
