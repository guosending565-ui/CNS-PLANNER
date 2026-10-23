"""Real SQLite/Shapely adapter regression; no QGIS or planning computation."""
import hashlib
import sqlite3
import struct
from types import SimpleNamespace
import pytest
from shapely.geometry import Polygon
from cns_planner.gis.source_inspection import inspect_geopackage, probe_vector_source_facts
from cns_planner.tasks.source_audit_task import _runner, signature


def fixture(tmp_path):
    path = tmp_path / "buildings.gpkg"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE gpkg_contents(table_name,data_type,srs_id,min_x,min_y,max_x,max_y)")
        db.execute("INSERT INTO gpkg_contents VALUES('buildings','features',4326,0,0,1,1)")
        db.execute("CREATE TABLE gpkg_geometry_columns(table_name,column_name,geometry_type_name)")
        db.execute("INSERT INTO gpkg_geometry_columns VALUES('buildings','geom','POLYGON')")
        db.execute("CREATE TABLE buildings(id INTEGER,source TEXT,height_m REAL,height_var REAL,height_status TEXT,geom BLOB)")
        db.execute("CREATE TABLE rtree_buildings_geom(id,minx,maxx,miny,maxy)")
        blob = struct.pack('<2sBBi', b'GP', 0, 1, 4326) + Polygon([(0,0),(1,0),(1,1),(0,0)]).wkb
        db.execute("INSERT INTO buildings VALUES(1,'test',10,0,'confirmed',?)", (blob,))
    return path


def test_source_audit_worker_uses_real_callback_adapter_and_keeps_source(tmp_path):
    path = fixture(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    staged, progress = [], []
    def stage(value, **kwargs):
        staged.append(value)
        return dict(artifact_id='test',relative_path='test.json',sha256='A'*64,size_bytes=1)
    ctx = SimpleNamespace(inputs={'snapshot': {'inputs': dict(role='buildings',path=str(path),
        previous={},mode='deep',signature=signature(path))}}, check_cancel=lambda:None,
        progress=lambda *args:progress.append(args), stage=stage)
    _runner(ctx)
    health = staged[0]['value']['details']['geometry_health']
    assert health['status'] == 'passed' and health['topology_checked'] is True
    assert progress
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert callable(probe_vector_source_facts)


def test_real_adapter_checks_cancel_before_geometry_scan(tmp_path):
    path = fixture(tmp_path)
    def cancel():
        raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        inspect_geopackage(path,'buildings',deep_geometry=True,cancel_check=cancel)
