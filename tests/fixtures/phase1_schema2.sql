-- Anonymous pre-revision schema2 layout; schema only, no source records.
CREATE TABLE memory_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE memory_records(
                        id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL,
                        scope TEXT NOT NULL, source TEXT NOT NULL, certainty TEXT NOT NULL,
                        owner TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
CREATE TABLE memory_edges(
                        child_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
                        parent_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
                        PRIMARY KEY(child_id,parent_id)
                    );
CREATE VIRTUAL TABLE memory_fts USING fts5(
                        memory_id UNINDEXED, tokens, tokenize='unicode61'
                    );
CREATE TABLE sessions (
            id TEXT PRIMARY KEY, client TEXT NOT NULL, host_session_id TEXT,
            owner_pid INTEGER, owner_identity TEXT, status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '', memory_ids TEXT NOT NULL DEFAULT '[]',
            opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, diagnostics TEXT NOT NULL DEFAULT '{}');
CREATE TABLE tasks (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, friend_id TEXT NOT NULL,
            client TEXT NOT NULL, request TEXT NOT NULL, workspace TEXT NOT NULL,
            status TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT '{}',
            result TEXT, error TEXT, idempotency_key TEXT UNIQUE,
            request_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE runs (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
            session_id TEXT NOT NULL, friend_id TEXT NOT NULL, friend_version INTEGER NOT NULL,
            client TEXT NOT NULL, status TEXT NOT NULL, can_write INTEGER NOT NULL,
            workspace TEXT NOT NULL, spec_json TEXT NOT NULL, token_hash TEXT NOT NULL,
            supervisor_pid INTEGER, supervisor_identity TEXT, child_pid INTEGER,
            child_identity TEXT, cli_session_id TEXT, event_count INTEGER NOT NULL DEFAULT 0,
            error TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, diagnostics TEXT NOT NULL DEFAULT '{}');
CREATE TABLE friend_registry (
                id TEXT PRIMARY KEY, version INTEGER NOT NULL, active INTEGER NOT NULL,
                digest TEXT NOT NULL, spec_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE friend_versions (
                friend_id TEXT NOT NULL, version INTEGER NOT NULL, spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(friend_id,version));
CREATE TABLE hook_turns (
            client TEXT NOT NULL, host_session_id TEXT NOT NULL,
            turn_started_at TEXT, checkpoint_at TEXT,
            PRIMARY KEY(client,host_session_id));
CREATE TABLE background_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE async_tasks(
          task_id TEXT PRIMARY KEY REFERENCES tasks(id), origin_thread TEXT,
          context TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
          notified_generation INTEGER NOT NULL DEFAULT -1);
CREATE TABLE connections(
          id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, mode TEXT NOT NULL,
          owner_pid INTEGER, owner_identity TEXT, connected_at TEXT NOT NULL,
          seen_at TEXT NOT NULL, expires_at REAL, state TEXT NOT NULL DEFAULT 'active');
CREATE TABLE notifications(
          id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
          origin_thread TEXT, target_thread TEXT, context TEXT NOT NULL,
          topic TEXT NOT NULL, message TEXT NOT NULL, task_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending', turn_id TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
          error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE daemon_state(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), pid INTEGER,
          identity TEXT, heartbeat REAL, status TEXT NOT NULL, error TEXT);
CREATE TABLE schedules (
                id TEXT PRIMARY KEY NOT NULL, spec_json TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                anchor_at TEXT NOT NULL, next_run_at TEXT, deleted_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE schedule_occurrences (
                id TEXT PRIMARY KEY NOT NULL,
                schedule_id TEXT NOT NULL REFERENCES schedules(id),
                revision INTEGER NOT NULL, scheduled_at TEXT NOT NULL,
                spec_json TEXT NOT NULL, task_id TEXT, created_at TEXT NOT NULL,
                UNIQUE(schedule_id,revision,scheduled_at));
CREATE TABLE slack_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE slack_channels(
          team_id TEXT NOT NULL,channel_id TEXT NOT NULL,mode TEXT NOT NULL DEFAULT 'mention',
          personal INTEGER NOT NULL DEFAULT 0,private_approved INTEGER NOT NULL DEFAULT 0,eligible INTEGER NOT NULL DEFAULT 1,
          thread_id TEXT,generation INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL DEFAULT '',
          last_activity REAL NOT NULL DEFAULT 0,last_ts TEXT,member_digest TEXT,error TEXT,
          PRIMARY KEY(team_id,channel_id));
CREATE TABLE slack_bindings(token_hash TEXT PRIMARY KEY,team_id TEXT NOT NULL,
          channel_id TEXT NOT NULL,generation INTEGER NOT NULL,personal INTEGER NOT NULL,
          revoked INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE slack_task_grants(task_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
          audience TEXT NOT NULL,reply_thread TEXT,input_id TEXT);
CREATE TABLE slack_schedule_grants(schedule_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
          audience TEXT NOT NULL);
CREATE TABLE slack_result_grants(task_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
          audience TEXT NOT NULL,notification_id TEXT NOT NULL,
          PRIMARY KEY(task_id,conversation_id,notification_id));
CREATE TABLE slack_inbox(id TEXT PRIMARY KEY,event_id TEXT UNIQUE,team_id TEXT NOT NULL,
          channel_id TEXT NOT NULL,message_ts TEXT NOT NULL,reply_ts TEXT,body TEXT NOT NULL,
          context TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',turn_id TEXT,thread_id TEXT,
          error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,origin_notice TEXT UNIQUE,
          generation INTEGER NOT NULL DEFAULT 0,UNIQUE(team_id,channel_id,message_ts));
CREATE TABLE slack_threads(team_id TEXT NOT NULL,channel_id TEXT NOT NULL,root_ts TEXT NOT NULL,
          PRIMARY KEY(team_id,channel_id,root_ts));
CREATE TABLE slack_outbox(id TEXT PRIMARY KEY,input_id TEXT NOT NULL,part INTEGER NOT NULL,
          body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',slack_ts TEXT,error TEXT,
          next_attempt REAL NOT NULL DEFAULT 0,blocks TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(input_id,part));
CREATE TABLE slack_approvals(id TEXT PRIMARY KEY,input_id TEXT NOT NULL,request_id TEXT NOT NULL,
          epoch TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
          expires_at REAL NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE slack_result_versions(notification_id TEXT NOT NULL, task_id TEXT NOT NULL,
      run_id TEXT, result_version TEXT NOT NULL, PRIMARY KEY(notification_id,task_id));
CREATE TABLE execution_routes(task_id TEXT PRIMARY KEY, context TEXT NOT NULL);
CREATE TABLE task_acceptance(
      task_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'not_ready',
      run_id TEXT, result_version TEXT, registered_at TEXT NOT NULL);
CREATE TABLE task_reviews(
      task_id TEXT NOT NULL, run_id TEXT NOT NULL, result_version TEXT NOT NULL,
      decision TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL,
      submitted_at TEXT NOT NULL, PRIMARY KEY(task_id,run_id,result_version));
CREATE TABLE context_refs(
      id TEXT PRIMARY KEY, principal TEXT NOT NULL, epoch TEXT NOT NULL,
      descriptor TEXT NOT NULL, digest TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE context_cursors(
      id TEXT PRIMARY KEY, ref_id TEXT NOT NULL REFERENCES context_refs(id) ON DELETE CASCADE,
      path TEXT NOT NULL, offset INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE monitor_instances(
      id TEXT PRIMARY KEY, kind TEXT NOT NULL, task_id TEXT, origin TEXT, input_id TEXT,
      context TEXT NOT NULL DEFAULT 'normal', audience TEXT, generation INTEGER,
      started REAL NOT NULL, heartbeat REAL, client_event REAL, progress REAL NOT NULL,
      phase TEXT NOT NULL DEFAULT 'working', phase_since REAL NOT NULL,
      finished REAL, state TEXT NOT NULL DEFAULT 'active');
CREATE TABLE monitor_progress(
      instance_id TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(instance_id,digest));
CREATE TABLE monitor_alerts(
      id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, type TEXT NOT NULL,
      created REAL NOT NULL, cleared REAL, notified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE read_retries(
      id TEXT PRIMARY KEY, operation TEXT NOT NULL, started REAL NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
      state TEXT NOT NULL, category TEXT);
CREATE TABLE metric_samples(
      id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL,
      queue_seconds REAL, execution_seconds REAL, delivery_seconds REAL,
      mcp_bytes INTEGER, previews INTEGER, pages INTEGER,
      input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
      reasoning_output_tokens INTEGER, total_tokens INTEGER);
CREATE TABLE runtime_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX memory_scope ON memory_records(scope, updated_at);
CREATE INDEX connections_thread ON connections(thread_id,state);
CREATE INDEX notification_pending ON notifications(status,next_attempt);
CREATE INDEX schedules_due
                ON schedules(next_run_at,id) WHERE enabled=1 AND deleted_at IS NULL;
CREATE INDEX schedule_occurrences_pending
                ON schedule_occurrences(scheduled_at,id) WHERE task_id IS NULL;
CREATE INDEX slack_pending ON slack_inbox(status,created_at);
CREATE UNIQUE INDEX monitor_alert_live ON monitor_alerts(instance_id,type) WHERE cleared IS NULL;
INSERT INTO memory_meta VALUES('schema_version','2');
INSERT INTO memory_meta VALUES('context_epoch','schema2-fixture');
INSERT INTO runtime_meta VALUES('schema_version','1');
INSERT INTO background_meta VALUES('schema_version','1');
INSERT INTO slack_meta VALUES('schema_version','1');
