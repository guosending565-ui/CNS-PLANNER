/**
 * Round38-SITE-1：CRS 人工确认门禁的前端契约回归。
 *
 * 只锁定两条"红线"契约（都是纯函数，无 DOM、无副作用）：
 *  1. 请求体的来源标注必须是人工工程裁定（user_confirmation / human_adjudication /
 *     user_supplied），**绝不**退化成 source_file_column / source_file_declaration；
 *  2. 起降点裁定档案必须携带既有证据指纹与"非数据提供方声明"的来源性质说明，
 *     且系统不预填取值（目标 CRS 必须由人明确输入）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {CRS_DOSSIERS,buildCrsConfirmPayload} from '../cns_planner/web/js/sources/source_center.js';

const LANDING='reference_landing_sites';

test('CRS 确认请求体：来源标注固定为人工裁定且不出现文件声明语义',()=>{
  const payload=buildCrsConfirmPayload({
    role:LANDING,value:'EPSG:4490',dossier:CRS_DOSSIERS[LANDING],
  });
  assert.equal(payload.role,LANDING);
  assert.equal(payload.value,'EPSG:4490');
  assert.equal(payload.source.type,'user_confirmation');
  assert.equal(payload.source.origin,'human_adjudication');
  assert.equal(payload.evidence.length,1);
  assert.equal(payload.evidence[0].type,'user_supplied');
  const body=JSON.stringify(payload);
  for(const forbidden of ['source_file_column','source_file_declaration','source_file']){
    assert.equal(body.includes(forbidden),false,`禁止出现 ${forbidden}`);
  }
});

test('CRS 确认请求体：证据清单随记录留存并可回溯到 SHA256',()=>{
  const payload=buildCrsConfirmPayload({
    role:LANDING,value:' EPSG:4490 ',dossier:CRS_DOSSIERS[LANDING],
  });
  assert.equal(payload.value,'EPSG:4490','取值必须去空白后再提交');
  const note=payload.evidence[0].note;
  assert.ok(note.includes('8a548706dba2c65bb46c1153b3e82473fa2d268582cd45e1dde23c6a2e53be24'));
  assert.ok(note.includes('895eede009a529ddc0ef1c9df204cf57cde4555e7ec766da9b47c5c38078af58'));
  assert.ok(note.includes('51432a8b7a401fa314b723314188da08e9582684d8115247dd2b1017321910fa'));
});

test('CRS 确认请求体：缺少取值必须直接拒绝，不得发出空值确认',()=>{
  assert.throws(()=>buildCrsConfirmPayload({role:LANDING,value:'   ',dossier:{}}),/取值/);
  assert.throws(()=>buildCrsConfirmPayload({role:'',value:'EPSG:4490',dossier:{}}),/role/);
});

test('起降点裁定档案：明确声明为项目内部裁定而非数据提供方声明',()=>{
  const dossier=CRS_DOSSIERS[LANDING];
  assert.equal(dossier.system_value,'EPSG:4490','取值框只允许作为提示预填');
  assert.ok(dossier.system_value_note.includes('提示'),'预填必须显式声明只是提示');
  assert.ok(dossier.origin_nature.includes('人工工程裁定'));
  assert.ok(dossier.origin_nature.includes('不是'));
  assert.ok(dossier.provenance_recording.includes('user_supplied'));
  assert.ok(dossier.provenance_recording.includes('source_file_column'));
  assert.ok(dossier.evidence.length>=4);
  assert.ok(dossier.pending_limits.length>=3);
});

test('参考航线 role 与起降点 role 的来源性质不得混用',()=>{
  assert.notEqual(
    CRS_DOSSIERS[LANDING].scope,
    CRS_DOSSIERS.reference_routes.scope,
  );
  assert.equal(CRS_DOSSIERS.reference_routes.scope,'source_file_declaration');
});
