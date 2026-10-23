/** R7-D2 signed research volume; coordinates already converted per vertex by canonical geoid. */
export function addFlightProtectionResearch(viewer, payload, C) {
  const entities = [];
  if (payload.status !== 'available') return { entities, status: 'blocked' };
  const position = (p) => C.Cartesian3.fromDegrees(...p);
  const color = (hex, alpha = 1) => C.Color.fromCssColorString(hex).withAlpha(alpha);
  const common = [
    ['证据等级', 'engineering_research_assumption'],
    ['formal_admission_evaluated', 'false'], ['overall_status', 'unknown'],
    ['研究范围', 'R0005 · 14点/13段 · 水平±20m · EGM2008正高80–120m'],
    ['输入指纹', payload.input_fingerprint],
    ['航路几何 SHA256', payload.geometry_sha256],
    ['垂直转换', '逐点 h=H+N（canonical EGM2008 → WGS84椭球高）'],
    ['CNS保障目标带', '左右各500m，独立图层'],
  ];
  function add(options, title, rows = [], kind = 'flight-protection-research') {
    const entity = viewer.entities.add(options);
    entity.__cns = { kind, title, rows: [...common, ...rows] };
    entities.push(entity);
    return entity;
  }
  function line(points, hex, width, title, rows = []) {
    return add({ polyline: { positions: points.map(position), width,
      material: color(hex), arcType: C.ArcType.NONE } }, title, rows);
  }
  for (const polygon of payload.polygons) {
    for (const [key, hex, height] of [['lower', '#32d9ff', 80], ['upper', '#baf7ff', 120]]) {
      const rings = polygon[key];
      const hierarchy = new C.PolygonHierarchy(rings[0].map(position),
        rings.slice(1).map((ring) => new C.PolygonHierarchy(ring.map(position))));
      add({ polygon: { hierarchy, perPositionHeight: true,
        material: color('#32d9ff', 0.16), arcType: C.ArcType.NONE } },
      `飞行保护区${height}m${key === 'lower' ? '下' : '上'}边界（工程假设）`);
      for (const ring of rings) line(ring, hex, 2, `保护区边界 ${height}m EGM2008`);
    }
    polygon.lower.forEach((ring, i) => {
      add({ wall: { positions: ring.map(position),
        minimumHeights: ring.map((p) => p[2]),
        maximumHeights: polygon.upper[i].map((p) => p[2]),
        material: color('#32d9ff', 0.22), outline: false } }, '飞行保护区侧壁（工程假设）');
    });
  }
  line(payload.centerline, '#ffdf60', 3, 'R0005 · 100m中心航迹（工程假设）');
  for (const [index, row] of payload.markers.entries()) {
    const minimum = index === 0;
    const title = minimum ? '第13段 · 最低下边界净空 2.760m' : '建筑4tCa7 · unknown';
    const hex = minimum ? '#ffdf60' : '#ff72dc';
    const details = [
      ['障碍 ID', row.obstacle_id], ['航段', row.segment_index],
      ['下边界净空（m）', row.protection_floor_clearance_m.toFixed(6)],
      ['100m航迹净空（m）', row.nominal_track_clearance_m.toFixed(6)],
      ['顶部正高（m）', row.top_egm2008_m],
      ['障碍证据', row.evidence_level], ['验证状态', row.status], ['验证原因', row.reason],
      ['局限', minimum ? '原生地形像元代表位置；非点障碍，不证明误差已被余量覆盖'
        : 'predicted高度，估计净空充裕仍为unknown'],
    ];
    line([row.top_position, row.floor_position], hex, 4, title, details);
    add({ position: position(row.top_position), point: {
      pixelSize: 13, color: color(hex), outlineColor: C.Color.BLACK, outlineWidth: 2,
      disableDepthTestDistance: Number.POSITIVE_INFINITY },
    label: { text: title, font: '14px sans-serif', fillColor: color(hex),
      showBackground: true, pixelOffset: new C.Cartesian2(12, -20),
      disableDepthTestDistance: Number.POSITIVE_INFINITY } }, title, details,
    minimum ? 'flight-protection-minimum' : 'flight-protection-unknown');
  }
  return { entities, status: 'available' };
}
