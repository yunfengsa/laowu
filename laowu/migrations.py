"""Schema v2, additive and idempotent; caller backs up before beginning."""
from .common import new_id

VERSION = 2


SCHEMA = '''
    CREATE TABLE IF NOT EXISTS friend_registry (
      id TEXT PRIMARY KEY, version INTEGER NOT NULL, active INTEGER NOT NULL,
      digest TEXT NOT NULL, spec_json TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS friend_versions (
      friend_id TEXT NOT NULL, version INTEGER NOT NULL, spec_json TEXT NOT NULL,
      created_at TEXT NOT NULL, PRIMARY KEY(friend_id,version));
    CREATE TABLE IF NOT EXISTS friend_management (
      friend_id TEXT PRIMARY KEY REFERENCES friend_registry(id),
      kind TEXT NOT NULL CHECK(kind IN ('static','dynamic')), template_id TEXT,
      ceiling_json TEXT NOT NULL DEFAULT '{}', personal INTEGER NOT NULL DEFAULT 0,
      needs_definition INTEGER NOT NULL DEFAULT 0, origin_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS friend_creations (
      key_hash TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
      friend_id TEXT NOT NULL REFERENCES friend_registry(id), revoked INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS slack_result_versions(notification_id TEXT NOT NULL, task_id TEXT NOT NULL,
      run_id TEXT, result_version TEXT NOT NULL, PRIMARY KEY(notification_id,task_id));
    CREATE TABLE IF NOT EXISTS execution_routes(task_id TEXT PRIMARY KEY, context TEXT NOT NULL, queued_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS task_acceptance(
      task_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'not_ready',
      run_id TEXT, result_version TEXT, registered_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS task_reviews(
      task_id TEXT NOT NULL, run_id TEXT NOT NULL, result_version TEXT NOT NULL,
      decision TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL,
      submitted_at TEXT NOT NULL, PRIMARY KEY(task_id,run_id,result_version));
    CREATE TABLE IF NOT EXISTS context_refs(
      id TEXT PRIMARY KEY, principal TEXT NOT NULL, epoch TEXT NOT NULL,
      descriptor TEXT NOT NULL, digest TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS context_cursors(
      id TEXT PRIMARY KEY, ref_id TEXT NOT NULL REFERENCES context_refs(id) ON DELETE CASCADE,
      path TEXT NOT NULL, offset INTEGER NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS monitor_instances(
      id TEXT PRIMARY KEY, kind TEXT NOT NULL, task_id TEXT, origin TEXT, input_id TEXT,
      context TEXT NOT NULL DEFAULT 'normal', audience TEXT, generation INTEGER,
      started REAL NOT NULL, heartbeat REAL, client_event REAL, progress REAL NOT NULL,
      phase TEXT NOT NULL DEFAULT 'working', phase_since REAL NOT NULL,
      finished REAL, state TEXT NOT NULL DEFAULT 'active');
    CREATE TABLE IF NOT EXISTS monitor_tools(instance_id TEXT NOT NULL, tool_id TEXT NOT NULL,
      phase TEXT NOT NULL, started REAL NOT NULL, finished REAL, PRIMARY KEY(instance_id,tool_id));
    CREATE TABLE IF NOT EXISTS monitor_progress(
      instance_id TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(instance_id,digest));
    CREATE TABLE IF NOT EXISTS monitor_alerts(
      id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, type TEXT NOT NULL,
      created REAL NOT NULL, cleared REAL, notified INTEGER NOT NULL DEFAULT 0);
    CREATE UNIQUE INDEX IF NOT EXISTS monitor_alert_live ON monitor_alerts(instance_id,type) WHERE cleared IS NULL;
    CREATE TABLE IF NOT EXISTS read_retries(
      id TEXT PRIMARY KEY, operation TEXT NOT NULL, started REAL NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
      state TEXT NOT NULL, category TEXT, owner_pid INTEGER, owner_identity TEXT);
    CREATE TABLE IF NOT EXISTS read_sources(kind TEXT NOT NULL,id TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(kind,id));
    CREATE TABLE IF NOT EXISTS metric_timings(subject TEXT PRIMARY KEY, kind TEXT NOT NULL,
      queued REAL, started REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS metric_samples(
      id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL,
      queue_seconds REAL, execution_seconds REAL, delivery_seconds REAL,
      mcp_bytes INTEGER, previews INTEGER, pages INTEGER,
      input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
      reasoning_output_tokens INTEGER, total_tokens INTEGER);

    '''

# Schema 2 shipped with several additive layouts. The revision distinguishes
# those layouts without making schema-1/2 snapshots incompatible.
LAYOUT_REVISION = 2
LAYOUT_KEY = 'phase1_layout_revision'
REPAIR_COLUMNS = {
    'runs': {'diagnostics': "TEXT NOT NULL DEFAULT '{}'", 'memory_policy_json': "TEXT NOT NULL DEFAULT '{}'"},
    'sessions': {'diagnostics': "TEXT NOT NULL DEFAULT '{}'"},
    'read_retries': {
        'owner_pid': 'INTEGER', 'owner_identity': 'TEXT',
        'total_attempts': 'INTEGER NOT NULL DEFAULT 0',
        'resumptions': 'INTEGER NOT NULL DEFAULT 0',
    },
    # No task timestamp means unknown, never the time this repair was run.
    'execution_routes': {'queued_at': 'TEXT'},
}


def _version(db):
    for table in ('runtime_meta', 'background_meta', 'slack_meta'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            marker = db.execute("SELECT value FROM " + table + " WHERE key='schema_version'").fetchone()
            if not marker or marker[0] != '1':
                raise ValueError('备份或数据库的扩展版本不兼容，请升级代码')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_meta'").fetchone():
        return 0
    marker = db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()
    version = int(marker[0]) if marker else 0
    if version > VERSION:
        raise ValueError('数据库版本较新，请升级代码')
    revision = db.execute('SELECT value FROM memory_meta WHERE key=?', (LAYOUT_KEY,)).fetchone()
    if revision and not 0 <= int(revision[0]) <= LAYOUT_REVISION:
        raise ValueError('数据库布局版本不兼容，请升级代码')
    return version


def needs_upgrade(db):
    """Read-only preflight, used before any startup schema mutation/backup."""
    version = _version(db)
    if version < VERSION:
        return True
    revision = db.execute('SELECT value FROM memory_meta WHERE key=?', (LAYOUT_KEY,)).fetchone()
    epoch = db.execute("SELECT value FROM memory_meta WHERE key='context_epoch'").fetchone()
    if not revision or int(revision[0]) != LAYOUT_REVISION or not epoch or not (epoch[0] or '').strip():
        return True
    objects = {r[0] for r in db.execute('SELECT name FROM sqlite_master')}
    # CREATE statements are the source of truth for all phase-one tables/indexes.
    import re
    required = re.findall(r'CREATE (?:UNIQUE )?(?:TABLE|INDEX) IF NOT EXISTS (\w+)', SCHEMA)
    if set(required) - objects:
        return True
    for table, additions in REPAIR_COLUMNS.items():
        columns = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
        if columns and set(additions) - columns:
            return True
    return False


def upgrade(db):
    if not needs_upgrade(db):
        return
    version = _version(db)
    db.execute('SAVEPOINT phase1_upgrade')
    try:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        _repair(db, legacy=version < VERSION)
        db.execute("RELEASE SAVEPOINT phase1_upgrade")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT phase1_upgrade")
        db.execute("RELEASE SAVEPOINT phase1_upgrade")
        raise


def _repair(db, *, legacy=False):
    for table, additions in REPAIR_COLUMNS.items():
        columns = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
        if not columns:
            continue  # Runtime tables may be initialized after memory.
        for name, definition in additions.items():
            if name in columns:
                continue
            db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + definition)
            if table == 'read_retries' and name == 'total_attempts':
                db.execute('UPDATE read_retries SET total_attempts=attempts')
            if table == 'execution_routes' and name == 'queued_at':
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
                    db.execute('UPDATE execution_routes SET queued_at=(SELECT created_at FROM tasks WHERE id=execution_routes.task_id)')
    # Existing file roles remain protected; only the historical confidant has
    # personal permission. Ignore any model-authored spec metadata.
    db.execute("""INSERT OR IGNORE INTO friend_management(friend_id,kind,personal)
        SELECT id,'static',CASE WHEN id='confidant' THEN 1 ELSE 0 END FROM friend_registry""")
    if legacy and db.execute("SELECT 1 FROM sqlite_master WHERE name='slack_result_grants'").fetchone():
        import json
        from .acceptance import result_version
        for row in db.execute('SELECT g.notification_id,g.task_id,t.result FROM slack_result_grants g JOIN tasks t ON t.id=g.task_id').fetchall():
            run = db.execute('SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1',(row['task_id'],)).fetchone()
            db.execute('INSERT OR IGNORE INTO slack_result_versions VALUES(?,?,?,?)',
                       (row['notification_id'],row['task_id'],run[0] if run else None,result_version(json.loads(row['result']) if row['result'] else None)))
    db.execute("INSERT OR IGNORE INTO memory_meta VALUES('context_epoch',?)", (new_id('epoch'),))
    db.execute("UPDATE memory_meta SET value=? WHERE key='context_epoch' AND (value IS NULL OR trim(value)='')", (new_id('epoch'),))
    db.execute("UPDATE memory_meta SET value='2' WHERE key='schema_version'")
    db.execute('INSERT OR REPLACE INTO memory_meta VALUES(?,?)', (LAYOUT_KEY, str(LAYOUT_REVISION)))



def invalidate(db, *, forget=False):
    db.execute("INSERT OR REPLACE INTO memory_meta VALUES('context_epoch',?)", (new_id('epoch'),))
    for table in ('context_cursors', 'context_refs', 'slack_result_versions', 'monitor_tools', 'monitor_progress', 'monitor_instances', 'monitor_alerts', 'read_retries', 'read_sources', 'metric_timings'):
        db.execute('DELETE FROM ' + table)
    if forget:
        db.execute('DELETE FROM task_reviews')
        db.execute("UPDATE task_acceptance SET state='not_ready',run_id=NULL,result_version=NULL")
        db.execute('DELETE FROM metric_samples')
        db.execute('DELETE FROM metric_timings')


def runtime_prepare(store):
    """Version the legacy runtime extension, including independently imported DBs.

    Some supported installations initialized memory before their runtime tables.
    Their memory schema alone cannot attest to the runtime column migration.
    """
    import contextlib
    with store._lock(), contextlib.closing(store.connect()) as db:
        meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_meta'").fetchone()
        if meta:
            row=db.execute("SELECT value FROM runtime_meta WHERE key='schema_version'").fetchone()
            if not row or row[0] != '1':
                raise ValueError('运行数据库版本较新或无效，请升级代码')
        missing=[]
        for table in ('runs','sessions'):
            columns={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
            for name, definition in REPAIR_COLUMNS[table].items():
                if columns and name not in columns:
                    missing.append((table, name, definition))
        groups_missing = (bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone())
                          and not db.execute("SELECT 1 FROM sqlite_master WHERE name='run_cleanup'").fetchone())
        if meta and not missing and not groups_missing:
            return
        if missing or groups_missing:
            store._backup_locked(db)
        db.execute('BEGIN IMMEDIATE')
        try:
            for table, name, definition in missing:
                db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
            if groups_missing:
                db.execute('CREATE TABLE run_cleanup (run_id TEXT PRIMARY KEY)')
                db.execute('INSERT INTO run_cleanup SELECT id FROM runs WHERE child_pid IS NOT NULL')
            db.execute('CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute("INSERT OR REPLACE INTO runtime_meta VALUES('schema_version','1')")
            db.commit()
        except BaseException:
            db.rollback()
            raise
