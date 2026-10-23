import json,gzip,sys
from pathlib import Path
import argparse
p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
O=args.output.resolve();B=args.baseline.resolve();assert O!=B and B not in O.parents and O not in B.parents, 'output must be separate from baseline';sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from cns_planner.algorithms.corridor_gap.v1 import _merge_union
load=lambda p:json.loads(p.read_text(encoding='utf8'))
m=load(O/'candidate_matrix.json');v=load(B/'p14_2km.json')['routes'][0]['voxels'];length=load(B/'p14_2km.json')['routes'][0]['route_length_m']
def longest(rows,field,alt=None):
 counts=[set() for _ in v]
 for r in rows:
  for i in r[field]:counts[i].add(r['physical_site_key'])
 raw=[{'start_m':max(0,float(x['nearest_route_offset_m'])-float(x['cell_half_diagonal_m'])),'end_m':min(length,float(x['nearest_route_offset_m'])+float(x['cell_half_diagonal_m']))} for i,x in enumerate(v) if (alt is None or x['altitude_layer_id']==alt) and len(counts[i])<(1 if x['surface_class']=='sea' else 2)]
 return max((b-a for a,b in _merge_union(raw)),default=0)
x={}
for c in 'CS':
 rows=[r for r in m['rows'] if r['subsystem']==c]
 x[c]={'baseline_longest_m':length,'all_geometry_potential_longest_m':longest(rows,'geometry_hit_indices'),'all_model_qualified_longest_m':longest(rows,'capability_qualified_indices'),'ALT100_geometry_longest_m':longest(rows,'geometry_hit_indices','ALT-100'),'ALT100_model_qualified_longest_m':longest(rows,'capability_qualified_indices','ALT-100')}
# Two explicit S probes, sorted by ALT100 hit count; recommendation only, no iterative optimization.
rows=sorted([r for r in m['rows'] if r['subsystem']=='S'],key=lambda r:(-sum(v[i]['altitude_layer_id']=='ALT-100' for i in r['capability_qualified_indices']),r['action_id']))
pair=[]
for r in rows:
 if r['physical_site_key'] not in {a['physical_site_key'] for a in pair}:pair.append(r)
 if len(pair)==2:break
x['minimum_probe']={'S_action_ids':[r['action_id'] for r in pair],'candidate_count':2,'height_layers':3,'voxels':2727,'ALT100_voxels':909,'P14_P15_batches':3,'description':'two single-candidate what-ifs plus one combined pair; no greedy iterations','pair_model_potential_longest_m':longest(pair,'capability_qualified_indices'),'C_probe_count':0,'C_reason':'resolve provider capability evidence before an effective-gain trial'}
s=load(B/'R0005-rev525/project_state.json');e=next(e for e in s['artifact_manifest']['entries'].values() if e['artifact_type']=='corridor.site_plan.detail');parts=json.loads(gzip.decompress((B/'R0005-rev525'/e['relative_path']).read_bytes()))['payload']['parts'];old=s['cns_corridor_site_plan_results']['selected_actions'];comparison=[]
for a in old:
 coord=a.get('coordinate');matches=[r for r in m['rows'] if r['subsystem']==a['subsystem'] and any(str(t) in str(a) for t in r['source_site_ids'])]
 comparison.append({'historical_action_id':a['action_id'],'historical_device_id':a['device_id'],'new_matches':[{'action_id':r['action_id'],'geometry_hits':len(r['geometry_hit_indices']),'qualified_hits':len(r['capability_qualified_indices']),'unknown':len(r['unknown_indices'])} for r in matches],'old_impact_reused':False})
(O/'continuous_potential.json').write_text(json.dumps(x,indent=2,ensure_ascii=False),encoding='utf8');(O/'historical_13_comparison.json').write_text(json.dumps(comparison,indent=2,ensure_ascii=False),encoding='utf8');print(json.dumps(x));print('historical',len(old))
