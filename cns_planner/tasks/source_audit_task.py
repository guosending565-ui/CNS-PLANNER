"""Read-only source verification in an isolated worker; no QGIS dependency."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import time

from .task_spec import TaskInputChanged, TaskRunResult, TaskSpec, WorkerResultRef, fingerprint_payload

SOURCE_AUDIT_TASK = 'source_audit_verify'
GEOMETRY_VERSION = 'gpkg-wkb-shapely-valid-v1'


def signature(path):
    stat = Path(path).stat()
    return {'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def evidence_identity(role, digest, health, version=GEOMETRY_VERSION):
    return fingerprint_payload({'role': role, 'sha256': digest,
                                'algorithm_version': version, 'geometry_health': health})


def reusable_evidence(role, digest, previous):
    evidence = previous.get('geometry_verification') or {}
    health = previous.get('geometry_health') or {}
    return bool(health.get('topology_checked') is True
                and health.get('status') in ('passed', 'blocked')
                and evidence.get('role') == role and evidence.get('sha256') == digest
                and evidence.get('algorithm_version') == GEOMETRY_VERSION
                and evidence.get('evidence_identity') == evidence_identity(role, digest, health))


def _inputs(state, payload):
    role = payload.get('role')
    path = payload.get('_source_path')
    if not role or not path:
        raise ValueError('数据源尚未在本机配置')
    return {'role': role, 'path': str(Path(path).resolve()), 'signature': signature(path),
            'mode': payload.get('mode', 'deep'), 'algorithm_version': GEOMETRY_VERSION,
            'previous': deepcopy(payload.get('_previous') or {})}


def _plan(state, payload):
    frozen = {key: payload[key] for key in ('role', '_source_path', 'mode') if key in payload}
    frozen['_previous'] = deepcopy(((state.get('source_audits') or {}).get('items') or {}).get(payload.get('role')) or {})
    return {'scope_id': _scope(state, frozen), 'worker_payload': frozen, 'inputs': _inputs(state, frozen)}


def _scope(state, payload):
    # One active verification per role, including identity/deep requests.
    return 'source_audit:' + str(payload.get('role') or '')


def _hash(path, context, start, span):
    size = max(1, Path(path).stat().st_size)
    digest, count = sha256(), 0
    with Path(path).open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            context.check_cancel()
            digest.update(chunk)
            count += len(chunk)
            context.progress(start + span * count / size, '全文件 SHA256 核验（需要读取全部文件）')
    return digest.hexdigest()


def _runner(context):
    inputs = context.inputs['snapshot']['inputs']
    role, path, previous = inputs['role'], inputs['path'], inputs['previous']
    started = time.monotonic()
    context.check_cancel()
    if signature(path) != inputs['signature']:
        raise TaskInputChanged('源文件签名已变化，未执行校验')
    digest = _hash(path, context, 0.01, 0.19)
    details, reused = {}, False
    if role in ('buildings', 'building_grid'):
        from ..gis.source_inspection import inspect_geopackage
        deep = inputs['mode'] == 'deep'
        reused = reusable_evidence(role, digest, previous)
        context.progress(0.21, '读取结构与元数据（含行数、空几何和高度字段扫描）')
        info = inspect_geopackage(
            path, role, deep_geometry=deep and not reused,
            cancel_check=context.check_cancel,
            on_progress=lambda done, total: context.progress(
                0.25 + 0.55 * done / max(1, total), f'深度几何拓扑校验 {done}/{total}'),
        )
        health = deepcopy(previous['geometry_health']) if reused else info['geometry_health']
        details = {'schema': {'fields': sorted(info['fields'])},
                   'feature_count': info['feature_count'], 'extent': info['extent'],
                   'declared_crs': info['crs'], 'geometry_health': health}
        if deep and not health.get('topology_checked'):
            raise ValueError('深度几何拓扑验证未完成，不能标记 verified')
        if deep or reused:
            details['geometry_verification'] = {
                'role': role, 'sha256': digest, 'algorithm_version': GEOMETRY_VERSION,
                'evidence_identity': evidence_identity(role, digest, health),
            }
        else:
            details['verification_depth'] = 'identity'
    context.check_cancel()
    # A second digest closes the scan-time mutation window; never trust stat alone.
    if _hash(path, context, 0.81, 0.17) != digest or signature(path) != inputs['signature']:
        raise TaskInputChanged('校验期间源文件改变，结果未发布')
    result = {'role': role, 'path': path, 'signature': inputs['signature'], 'sha256': digest,
              'details': details, 'reused': reused, 'mode': inputs['mode'],
              'previous_sha256': previous.get('sha256'),
              'elapsed_seconds': round(time.monotonic() - started, 3)}
    metadata = context.stage({'logical_key': SOURCE_AUDIT_TASK, 'value': result},
                             artifact_type='source.audit.verification')
    summary = {key: value for key, value in result.items() if key not in ('path', 'details', 'signature')}
    return TaskRunResult(summary=summary, staged=WorkerResultRef(
        artifact_id=str(metadata['artifact_id']), relative_path=str(metadata['relative_path']),
        sha256=str(metadata['sha256']), size_bytes=int(metadata['size_bytes']),
        artifact_type='source.audit.verification', summary=summary),
        message='已复用同 SHA 与同版本拓扑证据，等待发布' if reused else '数据源校验完成，等待发布')


SOURCE_AUDIT_SPEC = TaskSpec(
    task_type=SOURCE_AUDIT_TASK, task_name='数据源验证', message='数据源验证已排队',
    business_endpoint='/api/source-audits/verify', scope_key=_scope,
    input_snapshot=_inputs, submit_plan=_plan, runner=_runner, algorithm_types=(),
)
