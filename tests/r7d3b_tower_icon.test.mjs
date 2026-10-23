// R7-D3B：统一铁塔 PNG 图标（二维/三维）与高清底图接入的定向契约测试。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

import {
  drawTowerIcon, setTowerIconImages, towerIconUrls, towerSymbolSvg
} from '../cns_planner/web/js/map/display_layers.js';
import { mapLegendModel, renderCompactLegend } from '../cns_planner/web/js/workflow/map_legend.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = relative => fs.readFileSync(path.join(ROOT, relative), 'utf8');
const exists = relative => fs.existsSync(path.join(ROOT, relative));

function pngSize(relative) {
  const buffer = fs.readFileSync(path.join(ROOT, relative));
  assert.equal(buffer.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', `${relative} 必须是 PNG`);
  return {width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20)};
}

test('R7-D3B: the unified tower PNGs exist, keep transparency and are trimmed', () => {
  for (const name of ['tower_qinglv_128', 'tower_qinglv_unknown_128',
    'tower_qinglv_64', 'tower_qinglv_32', 'tower_qinglv_unknown_64',
    'tower_qinglv_unknown_32']) {
    const relative = `cns_planner/web/assets/symbols/${name}.png`;
    assert.ok(exists(relative), `缺少统一图标 ${relative}`);
    const size = pngSize(relative);
    assert.ok(size.width > 0 && size.height > 0);
    if (name.endsWith('_128')) {
      assert.equal(size.height, 128);
      assert.ok(size.width >= 100 && size.width <= 128, '128 档必须等比、去透明边');
    }
  }
});

test('R7-D3B: the legend embeds the same PNG inside its inline SVG', () => {
  const svg = towerSymbolSvg({size: 20, raster: true});
  assert.match(svg, /^<svg /);
  assert.match(svg, /tower_qinglv_128\.png/);
  assert.match(svg, /<image /);
  assert.match(svg, /<path /);
  assert.doesNotMatch(svg, /https?:\/\//);
  assert.deepEqual(Object.keys(towerIconUrls()).sort(), ['normal', 'unknown']);
});

test('R7-D3B: drawTowerIcon anchors the PNG at the tower base and falls back cleanly', () => {
  const calls = [];
  const ctx = {
    save() {}, restore() {}, imageSmoothingEnabled: false,
    drawImage(image, x, y, width, height) { calls.push({x, y, width, height}); },
  };
  const image = {naturalWidth: 114, naturalHeight: 128};
  setTowerIconImages({normal: image, unknown: image});
  assert.equal(drawTowerIcon(ctx, 100, 200, {sizePx: 22}), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].height, 22);
  assert.equal(calls[0].y, 178, 'PNG 必须底部锚定在塔址屏幕点');
  assert.equal(calls[0].x, 100 - calls[0].width / 2);
  assert.equal(drawTowerIcon(ctx, 0, 0, {sizePx: 0}), false);
});

test('R7-D3B: 3D towers use billboards with the same PNG and keep click details', () => {
  const app = read('cns_planner/web/js/visualization3d/app.js');
  assert.match(app, /TOWER_ICON_URL = '\/assets\/symbols\/tower_qinglv_128\.png/);
  assert.match(app, /TOWER_ICON_UNKNOWN_URL = '\/assets\/symbols\/tower_qinglv_unknown_128\.png/);
  assert.match(app, /billboard: \{/);
  assert.match(app, /verticalOrigin: Cesium\.VerticalOrigin\.BOTTOM/);
  assert.match(app, /scaleByDistance: new Cesium\.NearFarScalar/);
  assert.match(app, /entity\.__cns = \{/);
  assert.match(app, /kind: 'tower', hasTop/);
  assert.match(app, /towerIconStats\(\)/);
});

test('R7-D3B: the 2D hit test covers the PNG body without shrinking the old site radius', () => {
  const display = read('cns_planner/web/js/map/display_layers.js');
  assert.match(display, /entry\.hitRects=\[\[x-width\/2,y-size,x\+width\/2,y\]\]/);
  assert.match(display, /profileOf\(tower\.tower_id\)/);
  const clustering = read('cns_planner/web/js/map/point_clustering.js');
  assert.match(clustering, /const inIcon=\(entry\.hitRects\|\|\[\]\)\.some/);
});

test('R7-D3B: the 3D high-resolution basemap is a real switchable mode', () => {
  const html = read('cns_planner/web/visualization-3d.html');
  assert.match(html, /天地图高清影像/);
  assert.match(html, /towerIconNote/);
  const basemap = read('cns_planner/web/js/visualization3d/basemap.js');
  assert.match(basemap, /source\.coverage_crs84/);
  assert.match(basemap, /maximumLevel: \(source\.zoom \|\| \[4, 20\]\)\[1\]/);
  assert.match(basemap, /online_source_id/);
});

test('R7-D3C: the server requires an official server-side key and never spoofs headers', () => {
  const service = read('cns_planner/application/visualization_3d_imagery_service.py');
  assert.match(service, /CNS_TIANDITU_SERVER_KEY/);
  assert.match(service, /TIANDITU_BROWSER_KEY_ENV = "CNS_TIANDITU_TK"/);
  assert.doesNotMatch(service, /BROWSER_USER_AGENT/);
  assert.doesNotMatch(service, /"User-Agent":/);
  assert.doesNotMatch(service, /"Referer":/);
  assert.match(service, /no_user_agent_or_referer_spoofing/);
  const router = read('cns_planner/api/router.py');
  assert.match(router, /"\.png": "image\/png"/);
});

test('R7-D3C: known towers are labelled resolved-but-unconfirmed, not confirmed', () => {
  const html = read('cns_planner/web/visualization-3d.html');
  assert.match(html, /青绿色＝塔顶高度已解析、尚未权威确认（77 座）/);
  assert.match(html, /灰蓝＝塔顶未解析（296 座）/);
  const app = read('cns_planner/web/js/visualization3d/app.js');
  assert.match(app, /塔顶高度已解析、尚未权威确认，77 座级/);
  assert.match(app, /塔顶未解析，296 座/);

  const legend = mapLegendModel({
    flow: {
      towers: {status: 'passed', count: 373},
      tower_obstacle_profiles: {resolved_count: 77, unresolved_count: 296},
    },
  });
  const line = legend.groups.flatMap(group => group.lines)
    .find(item => item.id === 'tower-reference');
  assert.ok(line);
  assert.match(line.note, /青绿 77 座＝塔顶高度已解析、尚未权威确认/);
  assert.match(line.note, /灰蓝 296 座＝塔顶未解析/);
  assert.match(line.detail, /青绿 77 座＝塔顶高度已解析、尚未权威确认/);
  // 紧凑图例（地图上实际可见的那一套）也必须渲染语义，而不只是图标+名称。
  const compact = renderCompactLegend([{key: 'business', title: '业务', lines: [line]}],
    {collapsed: false, activeLayerCount: 1});
  assert.match(compact, /map-legend-detail/);
  assert.match(compact, /塔顶高度已解析、尚未权威确认/);
});
