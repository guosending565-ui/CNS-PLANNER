"""Read-only grid provenance contracts; fixture digests grant no execution rights."""
from copy import deepcopy
import gzip
from hashlib import sha256
import json
import pytest
from cns_planner.application.research_grid_materialization import materialize_grid


def encoded(logical_key, parts):
    return gzip.compress(json.dumps({'artifact_schema_version':1,
        'payload':{'logical_key':logical_key,'parts':parts}},sort_keys=True).encode(),mtime=0)


def fixture():
    cells=[{'grid_id':'MHT4063-L08-A','level':8,'bbox':[122.,29.,122.001,29.001],
            'center':[122.0005,29.0005]}]
    terrain={'MHT4063-L08-A':{'status':'missing_data','surface_elevation_mean_m':None}}
    buildings={'MHT4063-L08-A':{'status':'passed','height_max_m':None,'building_count':0}}
    blobs=[encoded('grid',{'cells':cells}),encoded('grid_attributes',{
        'terrain.cells':terrain,'buildings.cells':buildings,'airspace.cells':{'ignored':1}})]
    entries={}
    for kind,b in zip(['workspace.grid.cells','grid_attribute.cells'],blobs):
        h=sha256(b).hexdigest()
        entries[h]={'sha256':h,'artifact_id':h,'artifact_type':kind,'schema_version':1,
            'status':'published','content_encoding':'json.gz','size_bytes':len(b),
            'relative_path':'.cns-results/'+h+'.json.gz'}
    state={'revision':503,'grid':{'count':1,'level':8,'canonical_level':8,'cells':[]},
        'grid_attributes':{'terrain':{'vertical_reference':'egm2008_orthometric','cells':{}},
                           'buildings':{'cells':{}}},
        'spatial_3d':{'canonical_vertical_reference':'egm2008_orthometric'},
        'operational_routes':[{'route_id':'R0005','path_crs':'OGC:CRS84'}],
        'artifact_manifest':{'schema_version':1,'entries':entries}}
    raw=json.dumps(state).encode()
    return raw,{sha256(b).hexdigest():b for b in blobs}


def test_content_and_order_are_preserved_without_authority_or_airspace():
    raw,blobs=fixture(); before=deepcopy(blobs)
    a=materialize_grid(raw,blobs,expected_state_sha256=sha256(raw).hexdigest())
    b=materialize_grid(raw,blobs,expected_state_sha256=sha256(raw).hexdigest())
    assert a==b and blobs==before
    assert a['grid']['cells'][0]['grid_id']=='MHT4063-L08-A'
    assert a['grid_attributes']['terrain']['cells']['MHT4063-L08-A']['surface_elevation_mean_m'] is None
    assert a['grid_attributes']['buildings']['cells']['MHT4063-L08-A']['height_max_m'] is None
    assert set(a['grid_attributes'])=={'terrain','buildings'}
    assert a['manifest']['execution_authorized'] is False
    assert a['manifest']['crs_evidence_status']=='declared'
    assert len(a['manifest']['content_fingerprint'])==64


@pytest.mark.parametrize('change',['state_sha','missing_blob','blob_sha','count','duplicate',
                                    'missing_terrain','missing_buildings','vertical','bbox','level'])
def test_missing_or_changed_provenance_and_fields_fail_closed(change):
    raw,blobs=fixture(); state=json.loads(raw)
    if change=='state_sha':
        with pytest.raises(ValueError):materialize_grid(raw+b' ',blobs,expected_state_sha256=sha256(raw).hexdigest())
        return
    if change=='missing_blob':blobs.pop(next(iter(blobs)))
    elif change=='blob_sha':blobs[next(iter(blobs))]+=b'changed'
    elif change=='count':state['grid']['count']=8008
    elif change=='vertical':state['grid_attributes']['terrain']['vertical_reference']='egm96'
    else:
        key=next(k for k,v in state['artifact_manifest']['entries'].items()
                 if v['artifact_type']==('workspace.grid.cells' if change in ['duplicate','bbox','level'] else 'grid_attribute.cells'))
        v=json.loads(gzip.decompress(blobs.pop(key)))
        parts=v['payload']['parts']
        if change=='duplicate':parts['cells']*=2;state['grid']['count']=2
        elif change=='bbox':parts['cells'][0]['bbox']=[0,0,0,0]
        elif change=='level':parts['cells'][0]['level']=7
        elif change=='missing_terrain':parts['terrain.cells']={}
        elif change=='missing_buildings':parts['buildings.cells']={}
        blob=gzip.compress(json.dumps(v).encode(),mtime=0); new=sha256(blob).hexdigest()
        e=state['artifact_manifest']['entries'].pop(key)
        e.update(sha256=new,artifact_id=new,size_bytes=len(blob),relative_path='.cns-results/'+new+'.json.gz')
        state['artifact_manifest']['entries'][new]=e;blobs[new]=blob
    raw=json.dumps(state).encode()
    with pytest.raises(ValueError):materialize_grid(raw,blobs,expected_state_sha256=sha256(raw).hexdigest())
