const CAMERA_FIELDS = [
  'longitude', 'latitude', 'height', 'heading', 'pitch', 'roll',
];

export function readCameraState(camera, toDegrees) {
  if (!camera || !camera.positionCartographic || typeof toDegrees !== 'function') return null;
  const position = camera.positionCartographic;
  const state = {
    longitude: toDegrees(position.longitude),
    latitude: toDegrees(position.latitude),
    height: position.height,
    heading: camera.heading,
    pitch: camera.pitch,
    roll: camera.roll,
  };
  return CAMERA_FIELDS.every((field) => Number.isFinite(state[field])) ? state : null;
}

export function applyCameraState(camera, state, fromDegrees) {
  if (!camera || !state || typeof fromDegrees !== 'function') return false;
  if (!CAMERA_FIELDS.every((field) => Number.isFinite(state[field]))) return false;
  camera.setView({
    destination: fromDegrees(state.longitude, state.latitude, state.height),
    orientation: {
      heading: state.heading,
      pitch: state.pitch,
      roll: state.roll,
    },
  });
  return true;
}
