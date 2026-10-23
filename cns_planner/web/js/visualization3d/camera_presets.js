const preset = (label, longitude, latitude, height, heading, pitch, roll = 0) => Object.freeze({
  label, longitude, latitude, height, heading, pitch, roll,
});

export const CAMERA_PRESETS = Object.freeze({
  archipelago_overview: preset('舟山群岛全景', 122.18, 29.92, 105000, 0, -82),
  r0005_overhead: preset('R0005 区域高空俯瞰', 122.22778, 29.84306, 28000, 0, -90),
  taohua_oblique: preset('桃花岛 45° 斜视', 122.28, 29.81, 8500, 325, -45),
  coast_transition: preset('海岸山海过渡', 122.20, 29.88, 4200, 55, -28),
  port_near: preset('港口道路居民区近景', 122.30, 29.95, 2600, 215, -36),
  low_altitude: preset('低空近水平观察', 122.24, 29.84, 950, 38, -8),
});

export function flyToPreset(viewer, id, duration = 1.8) {
  const row = CAMERA_PRESETS[id];
  if (!viewer || !row) return false;
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(row.longitude, row.latitude, row.height),
    orientation: {
      heading: Cesium.Math.toRadians(row.heading),
      pitch: Cesium.Math.toRadians(row.pitch),
      roll: Cesium.Math.toRadians(row.roll),
    },
    duration,
  });
  return true;
}
