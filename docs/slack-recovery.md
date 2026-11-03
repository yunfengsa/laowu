# Slack 资格检查失败的受控恢复

本配方只适用于提交模型之前因资格检查参数错误而失败的输入。修复代码、重启或重新收取相同事件不会自动重排旧 failed 输入；同一事件仍由 inbox 去重。配方由离线合成数据库测试覆盖，不代表已执行任何真实恢复。

## 操作前提

1. 在已授权的本机维护流程中停止网关及自动拉起，确认没有在途执行，创建一致性备份。
2. 精确审核输入 ID 和失败原因，确认模型从未收到该输入。仅有 turn_id 为空不构成未提交证据。不批量恢复全部 failed，不读取客户端原始历史来猜测。
3. 使用修复版 `qualify(team, channel)` 的只读 Slack 检查重新核实身份、成员与资格，本地资格更新成功且 generation 与目标输入一致。不要手工改写 eligible、代际或重置频道来绕过核实。
4. 以下代码只定义函数。在既有 Store 上传入已审核的精确 ID。它要求 failed/invalid_arguments、thread/turn 为空、无 origin_notice/任务授权/审批，且只有一条从未尝试发送的 pending 失败提示；任一条件不符整批回滚。

<!-- slack-qualification-recovery:start -->
```python
def requeue_verified_qualification_failures(store, input_ids):
    import hashlib
    from laowu.common import now

    ids = tuple(input_ids)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('必须提供非空且不重复的精确输入 ID')
    failed_part = int(hashlib.sha256(b'failed').hexdigest()[:12], 16)
    with store.transaction() as db:
        for ident in ids:
            row = db.execute('SELECT id,team_id,channel_id,status,error,thread_id,turn_id,'
                             'origin_notice,generation,body FROM slack_inbox WHERE id=?', (ident,)).fetchone()
            if (not row or row['status'] != 'failed' or row['error'] != 'invalid_arguments'
                    or row['thread_id'] is not None or row['turn_id'] is not None
                    or row['origin_notice'] is not None or not row['body']):
                raise ValueError('输入不满足模型提交前资格检查失败条件')
            channel = db.execute('SELECT eligible,generation FROM slack_channels '
                                 'WHERE team_id=? AND channel_id=?',
                                 (row['team_id'], row['channel_id'])).fetchone()
            if not channel or not channel['eligible'] or channel['generation'] != row['generation']:
                raise ValueError('须重新核实频道资格且 generation 一致')
            if (db.execute('SELECT 1 FROM slack_task_grants WHERE input_id=?', (ident,)).fetchone()
                    or db.execute('SELECT 1 FROM slack_approvals WHERE input_id=?', (ident,)).fetchone()):
                raise ValueError('已有任务或审批，不自动恢复')
            outputs = db.execute('SELECT id,part,status,slack_ts,blocks FROM slack_outbox '
                                 'WHERE input_id=?', (ident,)).fetchall()
            if (len(outputs) != 1 or outputs[0]['part'] != failed_part
                    or outputs[0]['status'] != 'pending' or outputs[0]['slack_ts'] is not None
                    or outputs[0]['blocks'] != '[]'):
                raise ValueError('存在其他输出或投递可能性，不自动恢复')
            stamp = now()
            changed = db.execute("DELETE FROM slack_outbox "
                                 "WHERE id=? AND status='pending' AND slack_ts IS NULL",
                                 (outputs[0]['id'],)).rowcount
            claimed = db.execute("UPDATE slack_inbox SET status='pending',error=NULL,updated_at=? "
                                 "WHERE id=? AND status='failed' AND error='invalid_arguments' "
                                 "AND thread_id IS NULL AND turn_id IS NULL AND generation=?",
                                 (stamp, ident, row['generation'])).rowcount
            if changed != 1 or claimed != 1:
                raise ValueError('恢复前提已变化，整批回滚')
    return ids
```
<!-- slack-qualification-recovery:end -->

## 恢复后核对

函数在同一事务内删除已核实未发送的失败提示并将原输入改回 pending，保留原输入和事件标识、时间戳、回复位置与代际。不能留下 cancelled 旧失败提示占用固定的 failed part，也不能删除 sending/uncertain/sent 输出。随后按维护流程启动网关，确认原输入只提交一次并正常投递；若再次失败，先调查新原因。

无法确定动作是否已经提交时，只核对原动作回执，不能调用本配方重新发送。此函数不是常驻重试器。测试入口见 [验证说明](verification.md)，队列与凭据不得提交，见 [发布安全说明](publishing.md)。
