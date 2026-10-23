"""Read-only D21 screening; never invokes P16 plan/evaluate or a state writer."""
import json,sys,hashlib,math
from pathlib import Path
from collections import Counter,defaultdict
import argparse
p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
O=args.output.resolve();B=args.baseline.resolve();assert O!=B and B not in O.parents and O not in B.parents, 'output must be separate from baseline';O.mkdir(parents=True,exist_ok=True);sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from cns_planner.application.engineering_assumption_scenario_service import EngineeringAssumptionScenarioService
from cns_planner.application.corridor_site_planning_service import _apply_cumulative_action
from cns_planner.application.planning_evidence_service import device_catalog_with_evidence,aircraft_profile_with_evidence,_requirement_blocks
from cns_planner.algorithms.coverage.geometric_3d import build_geometric_providers,evaluate_geometry_point
from cns_planner.algorithms.service_capability.v1 import build_provider_devices,evaluate_capability_point,prepare_capability_context

def load(p):return json.loads(p.read_text(encoding='utf8'))
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def dump(name,x): (O/name).write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
def key(c):return ','.join(str(round(float(v),6)) for v in c)
paths=[B/'R0005-rev525/project_state.json',B/'p14_2km.json',B/'p15_2km.json',B/'selection.json']
before={str(p):digest(p) for p in paths}
s=load(paths[0]);vox=load(paths[1])['routes'][0]['voxels'];assert len(vox)==2727
assert sum(v['altitude_layer_id']=='ALT-100' for v in vox)==909
scenario=load(paths[3])['scenario']
svc=EngineeringAssumptionScenarioService(state_provider=lambda:s,standardized_facilities_provider=lambda:{},landing_sites_provider=lambda:{},tower_colocation_provider=lambda:s['tower_colocation_candidates'])
pool=svc.build_candidate_pool(scenario)
cat=device_catalog_with_evidence(s);air=aircraft_profile_with_evidence(s);req=_requirement_blocks(s)
towers={str(t['tower_id']):t for t in s['towers']['items']}
duplicate_keys={str(tid):'unresolved-colocation:'+str(g['longitude'])+','+str(g['latitude']) for g in s['towers']['metadata']['duplicate_coordinate_groups'] for tid in g['tower_ids']}
qualification=[]
for host in s['tower_colocation_candidates']['items']:
 tid=host['metadata']['host']['host_tower_id'];t=towers[str(tid)];v=host['vertical_profile'];comp=v.get('planning_origin_components',{})
 usable=v.get('planning_origin_status') in ('confirmed','estimated') and isinstance(v.get('planning_service_origin_egm2008_m'),(int,float))
 qualification.append({'tower_id':tid,'name':t.get('name'),'coordinate':host['coordinate'],'physical_site_key':duplicate_keys.get(str(tid),key(host['coordinate'])),'qualification':'research_assumption' if usable else 'insufficient_evidence','physical_mount_confirmed':False,'source_record':t.get('source'),'structure_height_m':comp.get('source_tower_height_m'),'installation_origin_status':v.get('planning_origin_status'),'installation_origin_egm2008_m':v.get('planning_service_origin_egm2008_m'),'terrain_and_height_components':comp,'vertical_profile':v,'physical_identity_status':'coordinate_group_only_independent_site_survey_pending'})
dump('tower_qualifications.json',qualification)
rows=[];union={c:[set() for v in vox] for c in 'CS'};qualified={c:[set() for v in vox] for c in 'CS'}
for action in pool['actions']:
 if action.get('source_kind')!='tower_colocation':continue
 c=action['subsystem'];coordkey=next((duplicate_keys[str(tid)] for tid in action['source_site_ids'] if str(tid) in duplicate_keys),key(action['coordinate']));action['distinct_site_id']='physical:'+coordkey
 facilities=_apply_cumulative_action({},action)
 providers=build_geometric_providers(facilities,cat)[c];devices=build_provider_devices(cat,facilities)
 requirement=req['communication' if c=='C' else 'surveillance'];context=prepare_capability_context(c,requirement,air)
 hit=[];ok=[];unknown=[];reason=Counter()
 for i,v in enumerate(vox):
  g=evaluate_geometry_point(v['probe'],providers)
  if g['covered'] is True:
   hit.append(i);union[c][i].add(coordkey)
   cap=evaluate_capability_point(c,{**v['probe'],**g},requirement,air,devices,context=context)
   if cap['status']=='meets_under_model':ok.append(i);qualified[c][i].add(coordkey)
   elif cap['status']=='unknown':unknown.append(i)
   reason.update(cap.get('reasons') or [])
  elif g['covered'] is None:unknown.append(i)
 rows.append({'action_id':action['action_id'],'subsystem':c,'device_id':action['device_id'],'source_site_ids':action['source_site_ids'],'physical_site_key':coordkey,'scenario_eligible':action['scenario_eligible'],'origin_status':action['origin_status'],'assumption_only':True,'real_world_confirmed':False,'geometry_hit_indices':hit,'capability_qualified_indices':ok,'unknown_indices':unknown,'reason_counts':dict(reason),'LOS':'not_evaluated','zero_geometry_reason':None if hit else 'outside_range_or_below_hemisphere_origin','zero_effective_reason':None if ok else ('capability_evidence_gate' if hit else 'geometry_gate')})
 print(c,action['site_id'],len(hit),len(ok),flush=True) if len(rows)%100==0 else None

def counts(lists,ids):return {'any_provider':sum(bool(lists[i]) for i in ids),'redundancy_satisfied':sum(len(lists[i]) >= (1 if vox[i]['surface_class']=='sea' else 2) for i in ids)}
summary={}
for c in 'CS':
 members=[r for r in rows if r['subsystem']==c];summary[c]={'candidate_count':len(members),'physical_sites':len({r['physical_site_key'] for r in members}),'geometry_positive_candidates':sum(bool(r['geometry_hit_indices']) for r in members),'capability_positive_candidates':sum(bool(r['capability_qualified_indices']) for r in members),'unknown_candidate_count':sum(bool(r['unknown_indices']) for r in members),'geometry_all':counts(union[c],range(len(vox))),'capability_all':counts(qualified[c],range(len(vox))),'geometry_ALT100':counts(union[c],[i for i,v in enumerate(vox) if v['altitude_layer_id']=='ALT-100']),'capability_ALT100':counts(qualified[c],[i for i,v in enumerate(vox) if v['altitude_layer_id']=='ALT-100'])}
dump('candidate_matrix.json',{'schema_version':'r7d21-2km-screening-v1','semantics':'P7/P8 per-candidate screening; not P16 candidate_impacts; LOS remains not_evaluated; model qualification is not field confirmation','voxel_ids':[v['voxel_id'] for v in vox],'rows':rows})
dump('summary.json',summary)
after={str(p):digest(p) for p in paths};assert before==after
identity={'source_sha256':before,'matrix_sha256':digest(O/'candidate_matrix.json'),'scenario':scenario,'qualification_distribution':dict(Counter(r['qualification'] for r in qualification)),'physical_site_count':len({r['physical_site_key'] for r in qualification}),'state_unchanged':True,'P16_iterations':0,'N':'unknown_unchanged','LOS':'not_evaluated_not_pass','input_protection':load(paths[3])['protected_before']}
dump('identity.json',identity);print(json.dumps(summary),flush=True)
