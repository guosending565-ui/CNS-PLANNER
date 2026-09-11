const test=require('node:test');
const assert=require('node:assert/strict');
const theme=require('../cns_planner/web/grid_theme.js');

test('quantile breaks and colors are deterministic',()=>{
  const breaks=theme.quantileBreaks([40,0,30,10,20],4);
  assert.deepEqual(breaks,[0,10,20,30,40]);
  assert.equal(theme.colorForValue(15,breaks,['a','b','c','d']),'b');
  assert.equal(theme.colorForValue(40,breaks,['a','b','c','d']),'d');
});

test('missing and non-finite values use the independent no-data color',()=>{
  const breaks=theme.quantileBreaks([null,undefined,'',0,10,Number.NaN],2);
  assert.deepEqual(breaks,[0,5,10]);
  assert.equal(theme.colorForValue(null,breaks,['a','b']),theme.NO_DATA_COLOR);
  assert.equal(theme.colorForValue(Number.NaN,breaks,['a','b']),theme.NO_DATA_COLOR);
});

test('WGS84 hit testing uses half-open cell boundaries',()=>{
  const bbox=[120,30,121,31];
  assert.equal(theme.bboxContainsHalfOpen(bbox,120,30),true);
  assert.equal(theme.bboxContainsHalfOpen(bbox,120.5,30.5),true);
  assert.equal(theme.bboxContainsHalfOpen(bbox,121,30.5),false);
  assert.equal(theme.bboxContainsHalfOpen(bbox,120.5,31),false);
  assert.equal(theme.bboxIntersects(bbox,[120.5,30.5,121.5,31.5]),true);
  assert.equal(theme.bboxIntersects(bbox,[121,30,122,31]),false);
});
