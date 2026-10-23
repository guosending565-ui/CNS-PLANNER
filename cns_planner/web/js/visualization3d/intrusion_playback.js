/**
 * Round37-T3D-3B 模拟轨迹回放（确定性时间基准 + Cesium 实体绑定）。
 *
 * 硬约定：
 *  * **唯一时间基准**：任何显示都来自 ``stateAtTime(model, t)``。播放与拖动因此
 *    必然显示同一状态；"事件是否发生"是时间 t 的纯函数，**绝不**依赖动画回调
 *    累加的计数器（那是本模块明确要避免的一类缺陷）。
 *  * 几何判定只有后端 ``assess_simulated_track`` 一套实现；本模块**不复制**任何
 *    Shapely 判定，只做插值、时间轴与实体绑定。
 *  * 三类语义严格分离：水平投影相交 / 示意三维保护体进入 / 监视探测。
 *    前两者都不允许被描述成"真实入侵"或"已探测"。
 *  * 正高（EGM2008）→ 椭球高必须逐点 ``h = H + N``；N 取不到时不画几何，
 *    **绝不**把正高直接当椭球高。
 */

export const PLAYBACK_SPEEDS = Object.freeze([0.25, 0.5, 1, 2, 4]);
export const DEFAULT_PLAYBACK_SPEED = 1;

/** 页面常驻标识：模拟、工程示意、非真实探测与运行许可。 */
export const INTRUSION_MODE_BANNER = 'SIMULATED · 工程示意 · 非真实探测与运行许可';

export const EVENT_SCOPE_HORIZONTAL = 'horizontal_projection_intersection';
export const EVENT_SCOPE_VOLUME = 'three_dimensional_volume_intrusion';

export const SCOPE_LABELS = Object.freeze({
  [EVENT_SCOPE_HORIZONTAL]: '水平投影相交',
  [EVENT_SCOPE_VOLUME]: '示意三维保护体',
});

const STATUS_TEXT = Object.freeze({
  horizontal: {
    intersects: '水平范围相交',
    boundary_contact: '仅接触水平边界（未进入内部）',
    clear: '水平范围未相交',
  },
  volume: {
    intrudes: '模拟进入工程示意保护体',
    boundary_contact: '仅接触保护体边界（未进入）',
    blocked: '三维判定缺证（blocked）',
    clear: '未进入示意保护体',
  },
});

/** 监视探测文案恒定：没有真实探测证据就只能是 not_evaluated。 */
export const SURVEILLANCE_TEXT = '监视探测：not_evaluated（无真实探测证据）';

function finiteNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

/** 把后端入侵合同的结果整理成回放模型（纯函数，无 Cesium 依赖）。 */
export function buildPlaybackModel(assessment) {
  if (!assessment || typeof assessment !== 'object') {
    throw new Error('intrusion assessment payload is required');
  }
  const rawNodes = Array.isArray(assessment.track_nodes) ? assessment.track_nodes : [];
  const nodes = [];
  for (const raw of rawNodes) {
    const longitude = finiteNumber(raw && raw.longitude);
    const latitude = finiteNumber(raw && raw.latitude);
    const altitudeEgm2008M = finiteNumber(raw && raw.altitude_egm2008_m);
    const relativeTimeS = finiteNumber(raw && raw.relative_time_s);
    if (longitude === null || latitude === null
        || altitudeEgm2008M === null || relativeTimeS === null) {
      throw new Error('track node is incomplete; refusing to interpolate a partial track');
    }
    nodes.push({ longitude, latitude, altitudeEgm2008M, relativeTimeS });
  }
  if (nodes.length < 2) throw new Error('at least two track nodes are required');
  for (let index = 1; index < nodes.length; index += 1) {
    if (nodes[index].relativeTimeS <= nodes[index - 1].relativeTimeS) {
      throw new Error('track relative times must be strictly increasing');
    }
  }

  const horizontal = assessment.horizontal_projection_intersection || {};
  const intrusion = assessment.three_dimensional_volume_intrusion || {};
  const detection = assessment.surveillance_detection || {};
  const horizontalEvents = normaliseEvents(horizontal.events, EVENT_SCOPE_HORIZONTAL);
  const volumeEvents = normaliseEvents(intrusion.events, EVENT_SCOPE_VOLUME);

  return {
    trackId: assessment.track_id || null,
    renderInstanceId: assessment.render_instance_id || null,
    simulated: assessment.simulated === true,
    eligibleForCurrentWarning: assessment.eligible_for_current_warning === true,
    startTimeS: nodes[0].relativeTimeS,
    endTimeS: nodes[nodes.length - 1].relativeTimeS,
    durationS: nodes[nodes.length - 1].relativeTimeS - nodes[0].relativeTimeS,
    nodes,
    horizontal: {
      status: horizontal.status || 'unknown',
      penetration: horizontal.penetration || 'none',
      boundaryContact: horizontal.boundary_contact === true,
    },
    intrusion: {
      status: intrusion.status || 'unknown',
      reason: intrusion.reason || null,
    },
    detection: {
      status: detection.status || 'not_evaluated',
      reason: detection.reason || null,
    },
    horizontalEvents,
    volumeEvents,
    events: mergeEvents(horizontalEvents, volumeEvents),
    geometry: assessment.protected_volume_geometry_status || null,
    trackGeometry: assessment.track_geometry || null,
    isolation: assessment.isolation || {},
  };
}

function normaliseEvents(list, scope) {
  const rows = [];
  for (const raw of Array.isArray(list) ? list : []) {
    const timeS = finiteNumber(raw && raw.relative_time_s);
    const longitude = finiteNumber(raw && raw.longitude);
    const latitude = finiteNumber(raw && raw.latitude);
    if (timeS === null || longitude === null || latitude === null) continue;
    rows.push({
      scope,
      kind: raw.kind === 'exit' ? 'exit' : 'entry',
      timeS,
      longitude,
      latitude,
      altitudeEgm2008M: finiteNumber(raw.altitude_egm2008_m),
      crossing: raw.crossing === true,
      crossingBasis: raw.crossing_basis || null,
      contact: raw.contact || null,
    });
  }
  return rows;
}

function mergeEvents(horizontalEvents, volumeEvents) {
  const rows = [...horizontalEvents, ...volumeEvents];
  // 稳定排序：同一时刻先水平后三维，再按进入先于离开。排序键完全确定，
  // 不依赖数组下标作为身份。
  const scopeOrder = { [EVENT_SCOPE_HORIZONTAL]: 0, [EVENT_SCOPE_VOLUME]: 1 };
  return rows
    .map((row, ordinal) => ({ row, ordinal }))
    .sort((left, right) => (
      left.row.timeS - right.row.timeS
      || scopeOrder[left.row.scope] - scopeOrder[right.row.scope]
      || (left.row.kind === right.row.kind ? 0 : (left.row.kind === 'entry' ? -1 : 1))
      || left.ordinal - right.ordinal))
    .map((entry) => entry.row);
}

/** 轨迹上任意时刻的位置（线性插值，与后端事件点使用同一参数化）。 */
export function positionAtTime(model, timeS) {
  const raw = finiteNumber(timeS);
  const time = clamp(raw === null ? model.startTimeS : raw, model.startTimeS, model.endTimeS);
  const nodes = model.nodes;
  let index = 0;
  while (index < nodes.length - 2 && time > nodes[index + 1].relativeTimeS) index += 1;
  const start = nodes[index];
  const end = nodes[index + 1];
  const span = end.relativeTimeS - start.relativeTimeS;
  const fraction = span > 0 ? clamp((time - start.relativeTimeS) / span, 0, 1) : 0;
  return {
    timeS: time,
    segmentIndex: index,
    fraction,
    longitude: start.longitude + fraction * (end.longitude - start.longitude),
    latitude: start.latitude + fraction * (end.latitude - start.latitude),
    altitudeEgm2008M: start.altitudeEgm2008M
      + fraction * (end.altitudeEgm2008M - start.altitudeEgm2008M),
  };
}

/** 某个语义在给定时刻是否处于区间内；完全由时间决定，与播放方式无关。 */
export function intervalStateAtTime(events, timeS) {
  const rows = Array.isArray(events) ? events : [];
  const active = [];
  for (let index = 0; index < rows.length; index += 1) {
    const entryEvent = rows[index];
    if (entryEvent.kind !== 'entry') continue;
    const exitEvent = rows[index + 1];
    if (!exitEvent || exitEvent.kind !== 'exit') continue;
    if (timeS >= entryEvent.timeS && timeS <= exitEvent.timeS) {
      active.push(entryEvent, exitEvent);
    }
  }
  return { inside: active.length > 0, active };
}

/**
 * 单一时间基准下的完整显示状态。播放、拖动、重置都必须消费它，
 * 因此同一 t 必然得到同一位置与同一事件状态。
 */
export function stateAtTime(model, timeS) {
  const position = positionAtTime(model, timeS);
  const horizontal = intervalStateAtTime(model.horizontalEvents, position.timeS);
  const volume = intervalStateAtTime(model.volumeEvents, position.timeS);
  return {
    timeS: position.timeS,
    elapsedS: position.timeS - model.startTimeS,
    durationS: model.durationS,
    progress: model.durationS > 0
      ? (position.timeS - model.startTimeS) / model.durationS : 0,
    position,
    horizontal: {
      status: model.horizontal.status,
      inside: horizontal.inside,
      active: horizontal.active,
      label: STATUS_TEXT.horizontal[model.horizontal.status] || '水平范围状态未知',
    },
    volume: {
      status: model.intrusion.status,
      reason: model.intrusion.reason,
      inside: volume.inside,
      active: volume.active,
      label: STATUS_TEXT.volume[model.intrusion.status] || '三维判定状态未知',
    },
    surveillance: {
      status: model.detection.status,
      reason: model.detection.reason,
      label: SURVEILLANCE_TEXT,
    },
  };
}

/**
 * 模拟轨迹回放控制器。
 *
 * ``clock`` 可注入（单位：秒），测试用假时钟即可完全确定地推进时间，
 * 不需要真实动画帧，也不需要计时器。
 */
export class PlaybackController {
  constructor({ model, clock = () => Date.now() / 1000, onUpdate = null } = {}) {
    if (!model) throw new Error('playback model is required');
    this.model = model;
    this.clock = typeof clock === 'function' ? clock : () => Date.now() / 1000;
    this.onUpdate = typeof onUpdate === 'function' ? onUpdate : null;
    this._playing = false;
    this._timeS = model.startTimeS;
    this._speed = DEFAULT_PLAYBACK_SPEED;
    this._lastClock = null;
    this.revision = 0;
  }

  get playing() { return this._playing; }

  get timeS() { return this._timeS; }

  get speed() { return this._speed; }

  get state() { return stateAtTime(this.model, this._timeS); }

  play() {
    if (this._timeS >= this.model.endTimeS) this._timeS = this.model.startTimeS;
    this._playing = true;
    this._lastClock = this.clock();
    this._emit();
    return this.state;
  }

  pause() {
    this._playing = false;
    this._lastClock = null;
    this._emit();
    return this.state;
  }

  toggle() { return this._playing ? this.pause() : this.play(); }

  setSpeed(value) {
    const candidate = Number(value);
    if (PLAYBACK_SPEEDS.includes(candidate)) this._speed = candidate;
    // 切换倍速时重设基准时钟，否则上一帧的经过时间会按新倍速重复计入。
    this._lastClock = this._playing ? this.clock() : null;
    this._emit();
    return this._speed;
  }

  seek(timeS) {
    const raw = finiteNumber(timeS);
    this._timeS = clamp(raw === null ? this._timeS : raw,
      this.model.startTimeS, this.model.endTimeS);
    this._lastClock = this._playing ? this.clock() : null;
    this._emit();
    return this.state;
  }

  /** 拖动时间轴：语义与 seek 完全一致（同一时间基准）。 */
  scrub(timeS) { return this.seek(timeS); }

  reset() {
    this._playing = false;
    this._timeS = this.model.startTimeS;
    this._lastClock = null;
    this._emit();
    return this.state;
  }

  /** 由渲染循环或测试的假时钟驱动；不做任何事件累加。 */
  tick() {
    if (this._playing) {
      const now = this.clock();
      const previous = this._lastClock === null ? now : this._lastClock;
      const delta = Math.max(0, now - previous);
      this._lastClock = now;
      this._timeS = Math.min(this.model.endTimeS, this._timeS + delta * this._speed);
      if (this._timeS >= this.model.endTimeS) this._playing = false;
    }
    this._emit();
    return this.state;
  }

  _emit() {
    this.revision += 1;
    if (this.onUpdate) this.onUpdate(this.state);
  }
}

// ----------------------------------------------------------- Cesium 实体绑定

function cartesianAt(deps, longitude, latitude, orthometricHeight) {
  const { Cesium, undulationAt } = deps;
  const undulation = typeof undulationAt === 'function' ? undulationAt(longitude, latitude) : null;
  if (!Number.isFinite(undulation)) return null;
  if (!Number.isFinite(orthometricHeight)) return null;
  return Cesium.Cartesian3.fromDegrees(longitude, latitude, orthometricHeight + undulation);
}

function eventRows(model) {
  return model.events.map((row, index) => ({ ...row, index }));
}

function eventPopup(row, model) {
  return {
    kind: 'simulated-intrusion-event',
    eventIndex: row.index,
    render_instance_id: model.renderInstanceId,
    title: `${SCOPE_LABELS[row.scope]} · ${row.kind === 'entry' ? '进入' : '离开'}`,
    rows: [
      ['语义', SCOPE_LABELS[row.scope]],
      ['事件', row.kind === 'entry' ? 'entry' : 'exit'],
      ['时间', `${row.timeS.toFixed(2)} s`],
      ['经纬度', `${row.longitude.toFixed(6)}, ${row.latitude.toFixed(6)}`],
      ['正高', row.altitudeEgm2008M === null ? '缺失' : `${row.altitudeEgm2008M.toFixed(2)} m EGM2008`],
      ['物理穿越', row.crossing ? '是' : `否（${row.crossingBasis || '未标注'}）`],
      ['保护体实例', model.renderInstanceId || '—'],
      ['免责', INTRUSION_MODE_BANNER],
    ],
  };
}

/** 创建模拟目标、轨迹线与事件标记；返回可复用的句柄（不重建 Viewer）。 */
export function addIntrusionEntities(viewer, model, deps = {}) {
  const { Cesium } = deps;
  if (!Cesium) throw new Error('Cesium dependency is required');
  const entities = [];
  const diagnostics = { trail_status: 'available', missing_undulation_nodes: 0 };

  const trailPositions = [];
  for (const node of model.nodes) {
    const point = cartesianAt(deps, node.longitude, node.latitude, node.altitudeEgm2008M);
    if (point === null) {
      diagnostics.missing_undulation_nodes += 1;
      continue;
    }
    trailPositions.push(point);
  }
  let trail = null;
  if (diagnostics.missing_undulation_nodes > 0) {
    // 缺 N 就不画轨迹：把正高直接当椭球高会造出一条并不存在的航迹。
    diagnostics.trail_status = 'egm2008_undulation_unavailable';
    trailPositions.length = 0;
  } else if (trailPositions.length >= 2) {
    trail = viewer.entities.add({
      polyline: {
        positions: trailPositions,
        width: 2.0,
        material: Cesium.Color.fromCssColorString('#7ad7ff').withAlpha(0.75),
        clampToGround: false,
      },
      name: `模拟航迹 ${model.trackId || ''}`,
    });
    trail.__cns = {
      kind: 'simulated-intrusion',
      render_instance_id: model.renderInstanceId,
      title: `模拟航迹 ${model.trackId || ''}`,
      rows: [
        ['模式', 'SIMULATED 工程示意'],
        ['轨迹节点', String(model.nodes.length)],
        ['时长', `${model.durationS.toFixed(1)} s`],
        ['保护体实例', model.renderInstanceId || '—'],
        ['免责', INTRUSION_MODE_BANNER],
      ],
    };
    entities.push(trail);
  } else {
    diagnostics.trail_status = 'insufficient_track_nodes';
  }

  const start = cartesianAt(deps, model.nodes[0].longitude, model.nodes[0].latitude,
    model.nodes[0].altitudeEgm2008M);
  const target = viewer.entities.add({
    position: start || Cesium.Cartesian3.fromDegrees(
      model.nodes[0].longitude, model.nodes[0].latitude, 0),
    point: {
      pixelSize: 11,
      color: Cesium.Color.fromCssColorString('#ffd166'),
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    name: '模拟目标',
  });
  target.__cns = {
    kind: 'simulated-intrusion-target',
    render_instance_id: model.renderInstanceId,
    title: '模拟目标（SIMULATED）',
    rows: [['免责', INTRUSION_MODE_BANNER]],
  };
  entities.push(target);

  // 三维进入时用竖直线段显示"当前正落在示意保护体的上下界之间"。
  const bar = viewer.entities.add({
    polyline: {
      positions: [],
      width: 3.0,
      material: Cesium.Color.fromCssColorString('#b26cff').withAlpha(0.95),
      clampToGround: false,
    },
    show: false,
    name: '示意保护体剖面',
  });
  bar.__cns = {
    kind: 'simulated-intrusion-volume',
    render_instance_id: model.renderInstanceId,
    title: '当前位于示意三维保护体内',
    rows: [['语义', '模拟进入工程示意保护体'], ['免责', INTRUSION_MODE_BANNER]],
  };
  entities.push(bar);

  const eventEntities = [];
  for (const row of eventRows(model)) {
    const position = cartesianAt(deps, row.longitude, row.latitude, row.altitudeEgm2008M);
    if (position === null) continue;
    const marker = viewer.entities.add({
      position,
      point: {
        pixelSize: 8,
        color: Cesium.Color.fromCssColorString(
          row.scope === EVENT_SCOPE_VOLUME ? '#b26cff' : '#ffb454'),
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: `${SCOPE_LABELS[row.scope]} ${row.kind}`,
    });
    marker.__cns = eventPopup(row, model);
    marker.__eventIndex = row.index;
    entities.push(marker);
    eventEntities.push(marker);
  }

  return {
    entities,
    eventEntities,
    trail,
    target,
    bar,
    model,
    volumeBounds: deps.volumeBounds || null,
    diagnostics,
    visible: true,
  };
}

/** 按单一时间基准的状态更新目标位置；只改属性，绝不增删/重建实体。 */
export function applyIntrusionState(handle, state, deps = {}) {
  if (!handle || !handle.target) return null;
  const { Cesium } = deps;
  const point = state.position;
  const target = cartesianAt(deps, point.longitude, point.latitude, point.altitudeEgm2008M);
  if (target === null) {
    handle.target.show = false;
    if (handle.bar) handle.bar.show = false;
    return { position: null, reason: 'egm2008_undulation_unavailable' };
  }
  handle.target.show = Boolean(handle.visible);
  handle.target.position = target;

  const bounds = handle.volumeBounds || null;
  const showBar = Boolean(handle.visible && state.volume.inside && bounds);
  if (handle.bar) {
    handle.bar.show = showBar;
    if (showBar) {
      const lower = cartesianAt(deps, point.longitude, point.latitude, bounds.lower);
      const upper = cartesianAt(deps, point.longitude, point.latitude, bounds.upper);
      handle.bar.polyline.positions = (lower && upper) ? [lower, upper] : [];
      handle.bar.show = Boolean(lower && upper);
    }
  }
  return { position: target, reason: null };
}

export function setIntrusionVisible(handle, visible) {
  if (!handle) return false;
  handle.visible = Boolean(visible);
  for (const entity of handle.entities) entity.show = handle.visible;
  return true;
}

/** 清除目标：实体全部移除，句柄不留任何引用。 */
export function removeIntrusionEntities(viewer, handle) {
  for (const entity of (handle && handle.entities) || []) viewer.entities.remove(entity);
  if (handle) {
    handle.entities = [];
    handle.eventEntities = [];
    handle.trail = null;
    handle.target = null;
    handle.bar = null;
  }
  return 0;
}

/**
 * 相机跟随 / 自由观察。
 *
 * 开启时**先量出当前相机相对目标的 heading/pitch/range**，再沿用同一组参数跟随，
 * 因此开启瞬间不会跳变；关闭时释放 lookAt 变换，回到自由观察。
 */
export function createCameraFollow(viewer, deps = {}) {
  const { Cesium } = deps;
  let range = null;
  let heading = 0;
  let pitch = Cesium.Math.toRadians(-45);
  let following = false;

  function measure(target) {
    const transform = Cesium.Transforms.eastNorthUpToFixedFrame(target);
    const inverse = Cesium.Matrix4.inverseTransformation(transform, new Cesium.Matrix4());
    const local = Cesium.Matrix4.multiplyByPoint(
      inverse, viewer.camera.positionWC, new Cesium.Cartesian3());
    const horizontal = Math.sqrt(local.x * local.x + local.y * local.y);
    const magnitude = Cesium.Cartesian3.magnitude(local);
    if (!Number.isFinite(magnitude) || magnitude <= 0) return;
    range = magnitude;
    heading = Math.atan2(local.x, local.y);
    pitch = -Math.atan2(local.z, Math.max(1e-6, horizontal));
  }

  return {
    get following() { return following; },
    get offset() { return { heading, pitch, range }; },
    enable(target, options = {}) {
      measure(target);
      if (Number.isFinite(options.range) && options.range > 0) range = options.range;
      if (Number.isFinite(options.heading)) heading = options.heading;
      if (Number.isFinite(options.pitch)) pitch = options.pitch;
      following = true;
      viewer.camera.lookAt(target, new Cesium.HeadingPitchRange(heading, pitch, range));
      return this.offset;
    },
    update(target) {
      if (!following || range === null) return false;
      viewer.camera.lookAt(target, new Cesium.HeadingPitchRange(heading, pitch, range));
      return true;
    },
    disable() {
      following = false;
      viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
      return true;
    },
  };
}

/** 时间轴的瞬时状态描述（供 HUD 与断言复用，纯函数）。 */
export function describeState(state) {
  return {
    timeText: `${state.timeS.toFixed(2)} s`,
    elapsedText: `${state.elapsedS.toFixed(2)} / ${state.durationS.toFixed(2)} s`,
    positionText: `${state.position.longitude.toFixed(6)}, `
      + `${state.position.latitude.toFixed(6)} @ ${state.position.altitudeEgm2008M.toFixed(1)} m`,
    horizontalText: state.horizontal.label,
    volumeText: state.volume.label,
    surveillanceText: state.surveillance.label,
    banner: INTRUSION_MODE_BANNER,
  };
}
