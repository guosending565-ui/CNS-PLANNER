function buildSpatialGrid(cells){
  if(!cells.length)return null;
  const bounds=[
    Math.min(...cells.map(item=>item.cell.bbox[0])),Math.min(...cells.map(item=>item.cell.bbox[1])),
    Math.max(...cells.map(item=>item.cell.bbox[2])),Math.max(...cells.map(item=>item.cell.bbox[3]))
  ];
  const columns=Math.min(64,Math.max(1,Math.ceil(Math.sqrt(cells.length)))),rows=columns,buckets=new Map();
  const width=(bounds[2]-bounds[0])/columns,height=(bounds[3]-bounds[1])/rows;
  const index=(value,start,span,count)=>Math.max(0,Math.min(count-1,Math.floor((value-start)/span)));
  for(const item of cells){
    const bbox=item.cell.bbox,x0=index(bbox[0],bounds[0],width,columns),x1=index(bbox[2],bounds[0],width,columns);
    const y0=index(bbox[1],bounds[1],height,rows),y1=index(bbox[3],bounds[1],height,rows);
    for(let x=x0;x<=x1;x++)for(let y=y0;y<=y1;y++){
      const key=x+':'+y;if(!buckets.has(key))buckets.set(key,[]);buckets.get(key).push(item);
    }
  }
  return {bounds,columns,rows,width,height,buckets,index};
}

export function buildGridOverlayCache(grid,attributes,risk,gridTheme){
  const sources={
    population:attributes?.population||{},terrain:attributes?.terrain||{},airspace:attributes?.airspace||{},
    traffic:attributes?.traffic||{},conflict:attributes?.conflict||{},buildings:attributes?.buildings||{}
  };
  const cells=(grid?.cells||[]).map(cell=>({
    cell,
    population:sources.population.cells?.[cell.grid_id]||null,
    terrain:sources.terrain.cells?.[cell.grid_id]||null,
    airspace:sources.airspace.cells?.[cell.grid_id]||null,
    traffic:sources.traffic.cells?.[cell.grid_id]||null,
    conflict:sources.conflict.cells?.[cell.grid_id]||null,
    buildings:sources.buildings.cells?.[cell.grid_id]||null,
    risk:risk?.cells?.[cell.grid_id]||null
  }));
  const usable=status=>status==='passed'||status==='missing_data';
  const populationValue=item=>Number.isFinite(item.population?.population_density_people_km2)
    ? item.population.population_density_people_km2
    : item.population?.value_mean;
  const values=(source,key)=>usable(source.status)?cells.map(item=>source===sources.population?populationValue(item):source===sources.buildings?item.buildings?.[key]:item.terrain?.[key]).filter(Number.isFinite):[];
  return {
    cells,byId:new Map(cells.map(item=>[item.cell.grid_id,item])),spatial:buildSpatialGrid(cells),
    populationBreaks:gridTheme.quantileBreaks(values(sources.population,'population_density_people_km2')),
    terrainBreaks:gridTheme.quantileBreaks(values(sources.terrain,'mean_elevation')),
    buildingCoverageBreaks:gridTheme.quantileBreaks(values(sources.buildings,'building_coverage_ratio')),
    buildingP95Breaks:gridTheme.quantileBreaks(values(sources.buildings,'height_p95_m')),
    buildingMaxBreaks:gridTheme.quantileBreaks(values(sources.buildings,'height_max_m'))
  };
}

export function findGridCell(cache,lon,lat,gridTheme){
  const spatial=cache?.spatial;if(!spatial)return null;
  const {bounds,columns,rows,width,height,buckets,index}=spatial;
  if(lon<bounds[0]||lon>=bounds[2]||lat<bounds[1]||lat>=bounds[3])return null;
  const candidates=buckets.get(index(lon,bounds[0],width,columns)+':'+index(lat,bounds[1],height,rows))||[];
  return candidates.find(item=>gridTheme.bboxContainsHalfOpen(item.cell.bbox,lon,lat))||null;
}
