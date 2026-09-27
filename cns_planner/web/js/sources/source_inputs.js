export const SOURCE_PATH_INPUTS = Object.freeze({
  basemap: 'basemapPath',
  population: 'populationPath',
  terrain: 'terrainPath',
  terrain_dtm: 'terrain_dtmPath',
  buildings: 'buildingsPath',
  building_grid: 'building_gridPath',
  land_mask: 'land_maskPath',
  reference_landing_sites: 'reference_landing_sitesPath',
  reference_routes: 'reference_routesPath',
  towers: 'towersPath',
});

export function syncSourcePathInputs(paths = {}, getNode = id => document.getElementById(id)) {
  for (const [role, id] of Object.entries(SOURCE_PATH_INPUTS)) {
    const input = getNode(id);
    if (input) input.value = paths?.[role] || '';
  }
}
