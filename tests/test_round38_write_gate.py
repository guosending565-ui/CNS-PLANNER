"""Round38: cancellation intent survives an in-flight worker record replace."""
from cns_planner.tasks.task_store import TaskStore, RUNNING

def test_cancel_intent_survives_stale_worker_heartbeat(tmp_path):
    store = TaskStore(tmp_path / 'tasks')
    record = store.create(task_type='source_audit_verify', scope_id='source_audit:buildings',
                          message='fixture', input_fingerprint='fixture', input_revision=0)
    task_id = record['task_id']
    store.claim(task_id, worker={'pid': 123})
    worker = TaskStore(tmp_path / 'tasks')
    stale_heartbeat = worker.get(task_id)
    assert stale_heartbeat['status'] == RUNNING
    store.request_cancel(task_id)
    # A worker had already read the old document before the parent cancelled.
    stale_heartbeat['progress'] = 0.2
    worker._write_json(worker._task_path(task_id), stale_heartbeat)
    recovered = TaskStore(tmp_path / 'tasks').get(task_id)
    assert recovered['cancel_requested'] is True
    assert recovered['status'] == 'cancelling'