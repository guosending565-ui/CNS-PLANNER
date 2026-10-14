/**
 * Round37-T3D-3B 模拟轨迹回放回归（任务书 §6 的第 14、15、16 项）。
 *
 * 14. 播放、暂停、拖动、重置在同一时间显示相同位置和事件状态；
 * 15. 清除目标后无残留实体；
 * 16. 不重建 Cesium Viewer。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_PLAYBACK_SPEED,
  EVENT_SCOPE_HORIZONTAL,
  EVENT_SCOPE_VOLUME,
  INTRUSION_MODE_BANNER,
  PLAYBACK_SPEEDS,
  PlaybackController,
  SURVEILLANCE_TEXT,
  addIntrusionEntities,
  applyIntrusionState,
  buildPlaybackModel,
  describeState,
  intervalStateAtTime,
  positionAtTime,
  removeIntrusionEntities,
  setIntrusionVisible,
  stateAtTime,
} from '../cns_planner/web/js/visualization3d/intrusion_playback.js';


const Cesium = {
  Cartesian3: {
    fromDegrees: (longitude, latitude, height) => ({ longitude, latitude, height }),
    magnitude: (value) => Math.hypot(value.x, value.y, value.z),
    subtract: (left, right, result) => {
      result.x = left.longitude - right.longitude;
      result.y = left.latitude - right.latitude;
      result.z = left.height - right.height;
      return result;
    },
  },
  Color: {
    BLACK: { css: '#000' },
    fromCssColorString: (css) => ({
      css,
      withAlpha(alpha) { return { css, alpha }; },
    }),
  },
  Math: { toRadians: (degrees) => (degrees * Math.PI) / 180 },
  Matrix4: {
    IDENTITY: Symbol('identity'),
    inverseTransformation: (_matrix, result) => result,
    multiplyByPoint: (_matrix, point) => point,
  },
  Transforms: { eastNorthUpToFixedFrame: (target) => target },
  HeadingPitchRange: class HeadingPitchRange {
    constructor(heading, pitch, range) { this.heading = heading; this.pitch = pitch; this.range = range; }
  },
};

/** 固定 undulation：只测回放逻辑，不测 geoid 取值。 */
const undulationAt = () => 15.5;

function fakeViewer() {
  const entities = {
    values: [],
    add(entity) { this.values.push(entity); return entity; },
    remove(entity) {
      const index = this.values.indexOf(entity);
      if (index >= 0) this.values.splice(index, 1);
      return index >= 0;
    },
  };
  return { entities, marker: Symbol('viewer') };
}

function event(kind, timeS, longitude, latitude, extra = {}) {
  return {
    kind, relative_time_s: timeS, longitude, latitude,
    altitude_egm2008_m: 100, crossing: true,
    crossing_basis: 'crossed_protected_volume_boundary_from_outside',
    contact: 'interior',
    ...extra,
  };
}

function assessment(overrides = {}) {
  const horizontalEvents = [
    event('entry', 2.5, 122.205, 29.905),
    event('exit', 7.5, 122.215, 29.915),
  ];
  const volumeEvents = [
    // t=4 s 落在第一段 80%（122.208 / 29.908），t=6 s 落在第二段 20%。
    event('entry', 4.0, 122.208, 29.908),
    event('exit', 6.0, 122.212, 29.912),
  ];
  return {
    schema_version: 'visualization-3d-intrusion-contract/v1',
    mode: 'engineering_scenario_preview',
    status: 'evaluated_simulation',
    simulated: true,
    track_id: 'SIM-PLAYBACK',
    render_instance_id: 'protectioninstancev1-abc',
    eligible_for_current_warning: false,
    track_nodes: [
      { longitude: 122.200, latitude: 29.900, altitude_egm2008_m: 100, relative_time_s: 0 },
      { longitude: 122.210, latitude: 29.910, altitude_egm2008_m: 100, relative_time_s: 5 },
      { longitude: 122.220, latitude: 29.920, altitude_egm2008_m: 100, relative_time_s: 10 },
    ],
    horizontal_projection_intersection: {
      status: 'intersects', penetration: 'interior', boundary_contact: false,
      events: horizontalEvents,
    },
    three_dimensional_volume_intrusion: {
      status: 'intrudes', reason: null, events: volumeEvents,
    },
    surveillance_detection: {
      status: 'not_evaluated',
      reason: 'real_surveillance_detection_evidence_missing',
      detection_evidence: null,
    },
    protected_volume_geometry_status: {
      horizontal: 'available', vertical: 'user_supplied_engineering_preview',
    },
    track_geometry: {
      initially_inside_horizontal: false, finally_inside_horizontal: false,
    },
    isolation: { enters_formal_p17: false, operational_authorization: false },
    ...overrides,
  };
}

function model(overrides = {}) {
  return buildPlaybackModel(assessment(overrides));
}

/** 只推进时间、不使用真实动画帧的可注入假时钟。 */
function fakeClock() {
  let now = 0;
  return {
    now: () => now,
    advance(seconds) { now += seconds; },
  };
}


// ------------------------------------------------------------------ 14. 同一时间基准

test('build playback model keeps one deterministic time base', () => {
  const built = model();

  assert.equal(built.startTimeS, 0);
  assert.equal(built.endTimeS, 10);
  assert.equal(built.durationS, 10);
  assert.equal(built.simulated, true);
  assert.equal(built.eligibleForCurrentWarning, false);
  assert.equal(built.horizontalEvents.length, 2);
  assert.equal(built.volumeEvents.length, 2);
  assert.equal(built.events.length, 4);
  assert.equal(built.events[0].scope, EVENT_SCOPE_HORIZONTAL);
  assert.equal(built.events[0].timeS, 2.5);
});

test('play pause seek and reset show identical state at the same time', () => {
  const built = model();
  const clock = fakeClock();
  const controller = new PlaybackController({ model: built, clock: clock.now });

  // 拖动到 5 s
  controller.scrub(5);
  const scrubbed = controller.state;

  // 播放推进到 5 s
  controller.reset();
  controller.play();
  clock.advance(5);
  controller.tick();
  controller.pause();
  const played = controller.state;

  // 直接 seek 到 5 s
  controller.seek(5);
  const sought = controller.state;

  assert.deepEqual(played, scrubbed);
  assert.deepEqual(sought, scrubbed);
  assert.deepEqual(controller.state, scrubbed);
  assert.equal(scrubbed.timeS, 5);
  assert.equal(scrubbed.horizontal.inside, true);
  assert.equal(scrubbed.volume.inside, true);
  assert.deepEqual(describeState(scrubbed), describeState(sought));
});

test('event state is a pure function of time and never an accumulated counter', () => {
  const built = model();

  // 以不同粒度推进到同一时刻，必须得到完全相同的状态。
  const stepwise = [];
  for (let time = 0; time <= 10.0001; time += 0.25) {
    stepwise.push(stateAtTime(built, Math.min(10, time)));
  }
  const direct = stateAtTime(built, 10);
  assert.deepEqual(stepwise[stepwise.length - 1], direct);

  const coarse = [];
  for (let time = 0; time <= 10.0001; time += 2.5) coarse.push(stateAtTime(built, time));
  assert.deepEqual(coarse[coarse.length - 1], direct);

  // 事件区间边界由时间唯一决定。
  assert.equal(stateAtTime(built, 2.4999).horizontal.inside, false);
  assert.equal(stateAtTime(built, 2.5).horizontal.inside, true);
  assert.equal(stateAtTime(built, 7.5).horizontal.inside, true);
  assert.equal(stateAtTime(built, 7.5001).horizontal.inside, false);
});

test('entry and exit events stay consistent in space and time', () => {
  const built = model();

  for (const row of built.events) {
    const position = positionAtTime(built, row.timeS);
    assert.ok(Math.abs(position.longitude - row.longitude) < 1e-9, 'longitude');
    assert.ok(Math.abs(position.latitude - row.latitude) < 1e-9, 'latitude');
    assert.ok(Math.abs(position.altitudeEgm2008M - row.altitudeEgm2008M) < 1e-9, 'altitude');
  }

  const interval = intervalStateAtTime(built.volumeEvents, 5);
  assert.equal(interval.inside, true);
  assert.equal(interval.active.length, 2);
  assert.equal(interval.active[0].kind, 'entry');
  assert.equal(interval.active[1].kind, 'exit');
});

test('playback speed switching and end-of-track behaviour are deterministic', () => {
  const built = model();
  const clock = fakeClock();
  const controller = new PlaybackController({ model: built, clock: clock.now });

  assert.equal(controller.speed, DEFAULT_PLAYBACK_SPEED);
  assert.deepEqual(PLAYBACK_SPEEDS, [0.25, 0.5, 1, 2, 4]);
  assert.equal(controller.setSpeed(4), 4);
  assert.equal(controller.setSpeed(3), 4, 'unsupported speed is rejected, not silently accepted');

  controller.play();
  clock.advance(1);
  controller.tick();
  assert.ok(Math.abs(controller.timeS - 4) < 1e-9, `expected 4 s at 4x, got ${controller.timeS}`);

  clock.advance(10);
  const end = controller.tick();
  assert.equal(end.timeS, built.endTimeS);
  assert.equal(controller.playing, false, 'playback stops exactly at the track end');

  // 停止后再按播放：从起点重播，而不是停在终点。
  controller.play();
  assert.equal(controller.timeS, built.startTimeS);
});

test('the three semantics keep separate, non-fabricating labels', () => {
  const crossing = describeState(stateAtTime(model(), 4.5));
  assert.equal(crossing.horizontalText, '水平范围相交');
  assert.equal(crossing.volumeText, '模拟进入工程示意保护体');
  assert.equal(crossing.surveillanceText, SURVEILLANCE_TEXT);
  assert.ok(!crossing.horizontalText.includes('入侵'));
  assert.ok(crossing.surveillanceText.includes('not_evaluated'));
  assert.equal(crossing.banner, INTRUSION_MODE_BANNER);

  const blocked = stateAtTime(model({
    three_dimensional_volume_intrusion: {
      status: 'blocked', reason: 'vertical_extent_unconfirmed', events: [],
    },
  }), 4.5);
  assert.ok(blocked.volume.label.includes('blocked'));
  assert.ok(!blocked.volume.label.includes('入侵'));
  assert.equal(blocked.volume.inside, false);

  const horizontalOnly = stateAtTime(model({
    three_dimensional_volume_intrusion: {
      status: 'boundary_contact',
      reason: 'boundary_contact_only_not_a_volume_entry', events: [],
    },
  }), 4.5);
  assert.ok(horizontalOnly.volume.label.includes('仅接触'));
  assert.ok(!horizontalOnly.volume.label.includes('入侵'));
});

test('historical exclusion and isolation flags survive into the playback model', () => {
  const built = model({
    eligible_for_current_warning: false,
    isolation: {
      historical_p17_excluded_from_current_warning: true,
      enters_formal_p17: false,
      enters_radar_or_rid_detection: false,
      operational_authorization: false,
    },
  });

  assert.equal(built.eligibleForCurrentWarning, false);
  assert.equal(built.isolation.enters_radar_or_rid_detection, false);
  assert.equal(built.isolation.operational_authorization, false);
  assert.equal(built.detection.status, 'not_evaluated');
});


// ---------------------------------------------------------------- 15. 清除无残留

test('clearing the target leaves no residual entity', () => {
  const viewer = fakeViewer();
  const built = model();
  const handle = addIntrusionEntities(viewer, built, {
    Cesium, undulationAt, volumeBounds: { lower: 90, upper: 125 },
  });

  assert.ok(handle.entities.length >= 4, 'trail + target + bar + 4 event markers');
  assert.equal(viewer.entities.values.length, handle.entities.length);

  removeIntrusionEntities(viewer, handle);

  assert.equal(viewer.entities.values.length, 0);
  assert.deepEqual(handle.entities, []);
  assert.deepEqual(handle.eventEntities, []);
  assert.equal(handle.target, null);
  assert.equal(handle.trail, null);
  assert.equal(handle.bar, null);
});

test('reset clears playback state without removing the entities', () => {
  const viewer = fakeViewer();
  const built = model();
  const clock = fakeClock();
  const handle = addIntrusionEntities(viewer, built, { Cesium, undulationAt });
  const controller = new PlaybackController({ model: built, clock: clock.now });
  const created = viewer.entities.values.length;

  controller.play();
  clock.advance(7);
  applyIntrusionState(handle, controller.tick(), { Cesium, undulationAt });
  const moved = handle.target.position;

  const state = controller.reset();
  applyIntrusionState(handle, state, { Cesium, undulationAt });

  assert.equal(controller.playing, false);
  assert.equal(state.timeS, built.startTimeS);
  assert.equal(viewer.entities.values.length, created, 'reset must not leave residual entities');
  assert.notDeepEqual(handle.target.position, moved);
  assert.deepEqual(handle.target.position, {
    longitude: built.nodes[0].longitude,
    latitude: built.nodes[0].latitude,
    height: built.nodes[0].altitudeEgm2008M + 15.5,
  });
});

test('hiding then clearing the simulated target removes every entity', () => {
  const viewer = fakeViewer();
  const handle = addIntrusionEntities(viewer, model(), { Cesium, undulationAt });

  setIntrusionVisible(handle, false);
  assert.ok(handle.entities.every((entity) => entity.show === false));
  setIntrusionVisible(handle, true);
  assert.ok(handle.entities.every((entity) => entity.show === true));

  removeIntrusionEntities(viewer, handle);
  assert.equal(viewer.entities.values.length, 0);
});

test('missing geoid undulation refuses to draw a fabricated trail', () => {
  const viewer = fakeViewer();
  const handle = addIntrusionEntities(viewer, model(), {
    Cesium, undulationAt: () => null,
  });

  assert.equal(handle.diagnostics.trail_status, 'egm2008_undulation_unavailable');
  assert.equal(handle.trail, null);
  const applied = applyIntrusionState(handle, stateAtTime(model(), 5), {
    Cesium, undulationAt: () => null,
  });
  assert.equal(applied.reason, 'egm2008_undulation_unavailable');
  assert.equal(handle.target.show, false);
});


// ------------------------------------------------------------ 16. 不重建 Viewer

test('playback never rebuilds the viewer and reuses the same entity instances', () => {
  const viewer = fakeViewer();
  const handle = addIntrusionEntities(viewer, model(), {
    Cesium, undulationAt, volumeBounds: { lower: 90, upper: 125 },
  });
  const identity = viewer.marker;
  const snapshot = [...viewer.entities.values];
  const controller = new PlaybackController({ model: handle.model, clock: () => 0 });

  for (const time of [0, 1.5, 2.5, 4, 5, 6, 7.5, 9, 10]) {
    controller.seek(time);
    applyIntrusionState(handle, controller.state, { Cesium, undulationAt });
  }

  assert.equal(viewer.marker, identity, 'viewer identity is stable');
  assert.equal(viewer.entities.values.length, snapshot.length);
  for (let index = 0; index < snapshot.length; index += 1) {
    assert.equal(viewer.entities.values[index], snapshot[index], `entity ${index} reused`);
  }
});

test('the volume bar only appears while the simulated target is inside the volume', () => {
  const viewer = fakeViewer();
  const built = model();
  const handle = addIntrusionEntities(viewer, built, {
    Cesium, undulationAt, volumeBounds: { lower: 90, upper: 125 },
  });

  applyIntrusionState(handle, stateAtTime(built, 1), { Cesium, undulationAt });
  assert.equal(handle.bar.show, false, 'outside the volume ⇒ no bar');

  applyIntrusionState(handle, stateAtTime(built, 5), { Cesium, undulationAt });
  assert.equal(handle.bar.show, true, 'inside the volume ⇒ bar shown');
  assert.equal(handle.bar.polyline.positions.length, 2);
  const [lower, upper] = handle.bar.polyline.positions;
  assert.ok(upper.height > lower.height);
});

test('track payload validation refuses partial or non-monotonic tracks', () => {
  const partial = assessment();
  delete partial.track_nodes[1].altitude_egm2008_m;
  assert.throws(() => buildPlaybackModel(partial), /incomplete/);

  const nonMonotonic = assessment();
  nonMonotonic.track_nodes[1].relative_time_s = 0;
  assert.throws(() => buildPlaybackModel(nonMonotonic), /strictly increasing/);

  const single = assessment();
  single.track_nodes = [single.track_nodes[0]];
  assert.throws(() => buildPlaybackModel(single), /at least two/);

  assert.throws(() => buildPlaybackModel(null), /required/);
});
