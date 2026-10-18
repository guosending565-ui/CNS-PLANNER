"""Round38 stability: source audit evidence and isolated worker regressions."""
from copy import deepcopy
from pathlib import Path
import threading
import time
import sqlite3

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.tasks.service import HeavyTaskService
from cns_planner.tasks.task_specs import has_task_type
from cns_planner.tasks.task_store import SUCCEEDED


def make_gpkg(path, rows=2):
    from test_building_environment_clearance import _gpkg
    from shapely.geometry import Polygon
    from shapely import to_wkb
    _gpkg(path, 'buildings', row=False)
    geometry = b'GP\x00\x01' + (4326).to_bytes(4, 'little') + to_wkb(Polygon([(121,30),(121.001,30),(121.001,30.001),(121,30)]))
    with sqlite3.connect(path) as connection:
        connection.executemany('INSERT INTO buildings VALUES (?,?,?,\'fixture\',12,2,\'valid\')',
                               [(n, geometry, str(n)) for n in range(1,rows+1)])
    return path


@pytest.fixture
def runtime(tmp_path):
    workflow=WorkflowService(tmp_path/'project.json',DEFAULTS)
    workflow.mutation_lock=threading.RLock()
    service=HeavyTaskService(workflow,tmp_path/'project.json',max_workers=1,poll_interval=0.05,heartbeat_interval=0.2)
    service.start()
    yield workflow,service
    service.stop()


def run(service,path,**payload):
    record,created=service.submit('source_audit_verify',{'role':'buildings','_source_path':str(path),**payload})
    result=service.wait_for_terminal(record['task_id'],timeout=30,interval=0.05)
    assert result['advanced']['status']=='succeeded',result
    return result

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / 'cns_planner/config/defaults.json'


def test_source_verification_registered_as_heavy_task():
    assert has_task_type('source_audit_verify')


def test_identical_verification_does_not_write_or_invalidate(tmp_path):
    source = tmp_path / 'source.csv'
    source.write_text('x,y\n122,30', encoding='utf-8')
    workflow = WorkflowService(tmp_path / 'project.json', DEFAULTS)
    workflow.verify_source('towers', source)
    before = deepcopy(workflow.state)
    disk = (tmp_path / 'project.json').read_bytes()
    workflow.verify_source('towers', source)
    assert workflow.state == before
    assert (tmp_path / 'project.json').read_bytes() == disk


def test_real_worker_success_reuse_and_no_revision_churn(runtime,tmp_path):
    workflow,service=runtime
    path=make_gpkg(tmp_path/'buildings.gpkg')
    result=run(service,path)
    audit=workflow.state['source_audits']['items']['buildings']
    assert audit['geometry_health']['topology_checked'] is True
    assert audit['geometry_health']['status']=='passed'
    assert not result['result_summary']['reused']
    before=deepcopy(workflow.state)
    result=run(service,path)
    assert result['result_summary']['reused'] is True
    assert workflow.state==before
    assert result['release']['summary']['state_unchanged'] is True
    result=run(service,path,mode='identity')
    assert result['result_summary']['reused'] is True
    assert workflow.state==before
    workflow.source_audit_service.register_quick('buildings',path,{'geometry_health':{'status':'not_fully_checked','topology_checked':False}})
    assert workflow.state['source_audits']['items']['buildings']['geometry_health']['topology_checked'] is True


def test_identity_only_never_impersonates_deep_topology(runtime,tmp_path):
    workflow,service=runtime
    path=make_gpkg(tmp_path/'buildings.gpkg')
    run(service,path,mode='identity')
    audit=workflow.state['source_audits']['items']['buildings']
    assert audit['status']=='identity_verified'
    assert audit['geometry_health']['topology_checked'] is False
    assert workflow.source_audit_service.result_snapshot({'buildings':path})['items']['buildings']['status']=='identity_verified'
    run(service,path)
    assert workflow.state['source_audits']['items']['buildings']['geometry_health']['topology_checked'] is True


def test_reverification_after_source_change_is_stable_on_first_repeat(runtime,tmp_path):
    workflow,service=runtime
    path=make_gpkg(tmp_path/'buildings.gpkg')
    run(service,path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE source_change_marker (note TEXT)')
    run(service,path)
    before=deepcopy(workflow.state)
    run(service,path)
    assert workflow.state==before


@pytest.mark.parametrize('field,value',[('sha256','wrong'),('role','other'),('algorithm_version','old'),('evidence_identity','tampered')])
def test_cache_requires_exact_evidence_identity(runtime,tmp_path,field,value):
    workflow,service=runtime
    path=make_gpkg(tmp_path/'buildings.gpkg')
    run(service,path)
    workflow.state['source_audits']['items']['buildings']['geometry_verification'][field]=value
    workflow.save()
    result=run(service,path)
    assert result['result_summary']['reused'] is False


def test_duplicate_and_queued_cancel_do_not_write(tmp_path):
    workflow=WorkflowService(tmp_path/'project.json',DEFAULTS)
    service=HeavyTaskService(workflow,tmp_path/'project.json',autostart_workers=False)
    path=make_gpkg(tmp_path/'buildings.gpkg')
    payload={'role':'buildings','_source_path':str(path)}
    record,created=service.submit('source_audit_verify',payload)
    again,new=service.submit('source_audit_verify',payload)
    assert created and not new and again['task_id']==record['task_id']
    service.cancel(record['task_id'])
    assert not service.active()
    assert workflow.state['revision']==0


def test_failed_worker_leaves_no_busy_and_can_retry(runtime,tmp_path):
    workflow,service=runtime
    path=tmp_path/'bad.gpkg';path.write_bytes(b'not sqlite')
    record,_=service.submit('source_audit_verify',{'role':'buildings','_source_path':str(path)})
    result=service.wait_for_terminal(record['task_id'],timeout=30,interval=0.05)
    assert result['advanced']['status']=='failed'
    assert not service.active()
    assert workflow.state['revision']==0
    path.unlink();make_gpkg(path)
    run(service,path)


def test_source_configuration_switch_discards_old_worker_result(runtime,tmp_path):
    workflow,service=runtime
    path=make_gpkg(tmp_path/'buildings.gpkg')
    workflow.source_audit_service.source_path_provider=lambda role:tmp_path/'other.gpkg'
    record,_=service.submit('source_audit_verify',{'role':'buildings','_source_path':str(path)})
    result=service.wait_for_terminal(record['task_id'],timeout=30,interval=0.05)
    assert result['advanced']['status']=='stale'
    assert workflow.state['revision']==0


def test_deep_scan_supports_cancellation_between_batches(tmp_path):
    from cns_planner.gis.source_inspection import inspect_geopackage
    from cns_planner.tasks.task_spec import TaskCancelled
    path=make_gpkg(tmp_path/'buildings.gpkg',2049)
    progress=[]
    def cancel():
        if progress:
            raise TaskCancelled()
    with pytest.raises(TaskCancelled):
        inspect_geopackage(path,'buildings',deep_geometry=True,cancel_check=cancel,on_progress=lambda done,total:progress.append((done,total)))
    assert progress[0][0]==1024


def test_worker_success_is_not_exposed_as_complete_until_publication(tmp_path):
    workflow=WorkflowService(tmp_path/'project.json',DEFAULTS)
    service=HeavyTaskService(workflow,tmp_path/'project.json',autostart_workers=False)
    path=make_gpkg(tmp_path/'buildings.gpkg')
    record,_=service.submit('source_audit_verify',{'role':'buildings','_source_path':str(path)})
    service.store.finish(record['task_id'],status=SUCCEEDED,message='worker done')
    assert service.find(record['task_id'])['advanced']['status']=='running'


def test_semantics_status_projection_does_not_traverse_artifact_detail():
    from cns_planner.application.result_currentness import apply_algorithm_semantics_stale
    class Detail:
        def __deepcopy__(self,memo):
            raise AssertionError('A status projection must not copy large detail trees')
    detail=Detail()
    stored={'algorithm_id':'old','algorithm_version':'0','status':'passed','voxels':detail}
    projected=apply_algorithm_semantics_stale(stored,'new','1')
    assert projected['status']=='stale'
    assert projected['stale_reason']=='algorithm_semantics_changed'
    assert stored['status']=='passed' and 'stale_reason' not in stored
    assert projected['voxels'] is detail


def test_candidate_readiness_projection_shares_readonly_mask_cells(tmp_path):
    class Detail:
        def __deepcopy__(self,memo):
            raise AssertionError('readiness must not copy mask cells')
    workflow=WorkflowService(tmp_path/'project.json',DEFAULTS)
    cells={'cell':{'detail':Detail()}}
    workflow.state['layered_route_candidates']['masks']={'old':{'cells':cells,'status':'passed'}}
    projected=workflow.layered_route_planner_service.result_snapshot()
    assert projected['masks']['old']['cells'] is cells
    assert projected['masks']['old']['current_applicability']=='stale'
    assert 'current_applicability' not in workflow.state['layered_route_candidates']['masks']['old']


def test_slim_candidate_projection_never_copies_discarded_diagnostics():
    from cns_planner.application.workflow_service import slim_layered_route_candidates
    class Detail:
        def __deepcopy__(self,memo):
            raise AssertionError('discarded diagnostics must not be copied')
    collection={'items':[],'masks':{'mask':{'cells':{'g':{'status':'unknown','diagnostic':Detail()}}}}}
    projected=slim_layered_route_candidates(collection)
    assert projected['masks']['mask']['cells']['g']['status']=='unknown'
    assert 'diagnostic' not in projected['masks']['mask']['cells']['g']


def test_transient_windows_read_error_never_hides_an_existing_task(tmp_path,monkeypatch):
    workflow=WorkflowService(tmp_path/'project.json',DEFAULTS)
    service=HeavyTaskService(workflow,tmp_path/'project.json',autostart_workers=False)
    path=make_gpkg(tmp_path/'buildings.gpkg')
    record,_=service.submit('source_audit_verify',{'role':'buildings','_source_path':str(path)})
    task_path=service.store._task_path(record['task_id'])
    original=Path.read_text
    failures=[]
    def read(current,*args,**kwargs):
        if current==task_path and not failures:
            failures.append(True)
            raise PermissionError('simulated atomic replace sharing race')
        return original(current,*args,**kwargs)
    service.store._stamp=None
    monkeypatch.setattr(Path,'read_text',read)
    assert service.find(record['task_id']) is not None


def test_workflow_review_projection_omits_frozen_detail_but_keeps_gates_and_unknown_counts():
    from cns_planner.application.workflow_service import _snapshot_detail_projection
    heavy={'voxels':['large-detail']*1000}
    review={'status':'stale','selected_variant_id':'v','variants':[{'variant_id':'v','evaluation':{
        'authoritative_hypothetical':heavy,'before_p15':heavy,'after_p15':heavy,
        'confirmation_gate':{'status':'unknown'},'continuous_service_projection':{'projected_status':'unknown'},
        'comparison_matrix':[{'unknown_voxel_ids':['a','b'],'objective_status':'unknown'}]}}]}
    source={'cns_plan_review':review,'cns_continuous_service':{'result':{'status':'unknown','routes':[]}}}
    projected=_snapshot_detail_projection(source)
    evaluation=projected['cns_plan_review']['variants'][0]['evaluation']
    assert evaluation['authoritative_hypothetical']['voxels_count']==1000
    assert 'voxels' not in evaluation['before_p15'] and 'voxels' not in evaluation['after_p15']
    assert evaluation['confirmation_gate']['status']=='unknown'
    assert evaluation['comparison_matrix'][0]['unknown_voxel_ids_count']==2
    assert projected['cns_continuous_service']['result']['status']=='unknown'
    assert review['variants'][0]['evaluation']['before_p15'] is heavy
