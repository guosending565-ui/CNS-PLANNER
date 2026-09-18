import {escapeHtml,shell,statusText} from './common.js';
export function render({flow,draftWorkspace,gridDisplay,populationDisplayLabel,formatNumber}){
  const workspace=flow.workspace,health=workspace?.health,grid=flow.grid,attributes=flow.grid_attributes||{},risk=flow.grid_risk||{},spatial=flow.spatial_3d||{},layers=spatial.altitude_layers||[];
  const buildings=attributes.buildings||{};
  let summary='<div class="empty-note">尚未保存工作区。点击“框选工作区”后在地图拖出矩形。</div>';
  if(workspace)summary='<div class="metric-grid"><b>'+workspace.area_km2.toLocaleString()+' km²<small>工作区面积</small></b><b>'+statusText(health.population.status)+'<small>人口数据</small></b><b>'+statusText(health.airspace.status)+'<small>空域数据</small></b><b>'+health.loaded_layer_count+'<small>已加载图层</small></b></div><div class="missing-list">DEM：'+statusText(health.terrain?.status||'missing_data')+' · 建筑：missing_data · 财产：missing_data</div><div class="flow-summary">'+escapeHtml(health.terrain?.message||'DEM 状态未知')+'<br>'+(grid?.status==='passed'?'MH/T 4063.1 标准网格：L'+grid.level+' · '+grid.count+' 格'+(grid.coarsened?'（已按数量限制降级）':''):'标准网格：未生成')+'<br>人口映射：'+statusText(attributes.population?.status==='stale'?'stale':attributes.population?.value_status||attributes.population?.status||'not_calculated')+' · full '+(attributes.population?.full_count||0)+' / partial '+(attributes.population?.partial_count||0)+' / missing '+(attributes.population?.missing_count||0)+' / outside '+(attributes.population?.outside_count||0)+'<br>DEM 映射：'+statusText(attributes.terrain?.status||'not_calculated')+' · 已处理 '+(attributes.terrain?.count||0)+' 格（有效 '+(attributes.terrain?.covered_count||0)+' 格）<br>空域映射：'+statusText(attributes.airspace?.status||'not_calculated')+' · 已处理 '+(attributes.airspace?.count||0)+' 格（命中 '+(attributes.airspace?.hit_count||0)+' 格）<br>交通暴露：'+statusText(attributes.traffic?.status||'not_calculated')+' · 已处理 '+(attributes.traffic?.count||0)+' 格（命中 '+(attributes.traffic?.covered_count||0)+' 格）<br>冲突暴露：'+statusText(attributes.conflict?.status||'not_calculated')+' · 已处理 '+(attributes.conflict?.count||0)+' 格（命中 '+(attributes.conflict?.covered_count||0)+' 格）<br>相对风险：'+statusText(risk.status||'not_calculated')+' · 完整度 '+formatNumber((risk.data_completeness||0)*100)+'%</div>';
  const checked=value=>gridDisplay.theme===value?'checked':'';
  const ids={none:'gridThemeNone',population:'gridPopulationTheme',terrain:'gridTerrainTheme'};
const themeOptions = [
  ['none', '无'],
  ['population', '人口密度'],
  ['terrain', '地形 DEM'],
  ['building_density', '建筑密度'],
  ['building_p95', 'P95 建筑高度'],
  ['building_max', '最大建筑高度'],
  ['traffic_exposure', '交通暴露'],
  ['conflict_exposure', '冲突暴露'],
  ['ground_risk', '地面风险'],
  ['overall_risk', '综合风险'],
];

const themes = `
  <div class="grid-theme-controls">

    <div class="grid-control-title">网格显示</div>

    <label class="grid-outline">
      <input
        type="checkbox"
        id="gridOutlineToggle"
        ${gridDisplay.outline ? 'checked' : ''}
      >
      <span>标准网格</span>
    </label>

    <div class="grid-theme-title">专题模式</div>

    <div class="grid-theme-options">
      ${themeOptions.map(([value, label]) => `
        <label class="grid-theme-option">
          <input type="radio" name="gridThemeMode"
            id="${ids[value] || 'gridTheme-' + value}"
            value="${value}"
            ${checked(value)}
          >
          <span>${label}</span>
        </label>
      `).join('')}
    </div>

    <small class="grid-theme-note">
      ${populationDisplayLabel(attributes.population)}
    </small>

  </div>
`;  const draft=draftWorkspace?'<div class="flow-summary">待保存：'+draftWorkspace.map(value=>value.toFixed(5)).join(', ')+'</div>':'';
  const buildingSummary='<div class="flow-summary"><b>建筑环境链</b><br>FABDEM DTM：'+statusText(health?.terrain_dtm?.status||'missing_data')+' · GBA buildings：'+statusText(health?.buildings?.status||'missing_data')+' · L8 grid：'+statusText(health?.building_grid?.status||'missing_data')+'<br>建筑网格映射：'+statusText(buildings.status||'not_calculated')+' · '+(buildings.covered_count||0)+' / '+(buildings.count||0)+' 格<br>0 表示覆盖范围内确认无该项；灰色表示无数据/范围外。建筑风险权重保持 0，净空评估独立。</div><label>目标标准网格层级<select id="workspaceGridLevel"><option value="8" '+((grid?.preferred_level??8)===8?'selected':'')+'>L8（建筑环境直接映射）</option><option value="7" '+(grid?.preferred_level===7?'selected':'')+'>L7（建筑网格 unsupported）</option><option value="6" '+(grid?.preferred_level===6?'selected':'')+'>L6（建筑网格 unsupported）</option></select></label><small>若工作区过大超过网格数量限制，系统仍会降级层级，此时不会聚合/插值 L8 建筑高度。</small>';
  const layerRows=layers.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.name)+'</b><small>'+escapeHtml(item.altitude_layer_id)+' · '+item.lower_altitude_m+'–'+item.upper_altitude_m+' m · '+escapeHtml(item.vertical_reference)+'</small></span></div>').join('');
  const vertical='<h3>3D 高度层</h3><div class="flow-summary">内部 canonical vertical datum：EGM2008 orthometric。高度层不预生成 voxel。</div><div class="panel-file-input"><input class="panel-input" id="altitudeLayerId" placeholder="layer id" value="L-LOW"><select id="altitudeReference"><option value="egm2008_orthometric">EGM2008 orthometric</option><option value="agl">AGL</option><option value="wgs84_ellipsoidal">WGS84 ellipsoidal</option><option value="unknown">unknown</option></select></div><div class="button-row"><input class="panel-input" type="number" id="altitudeLower" value="0"><input class="panel-input" type="number" id="altitudeUpper" value="120"></div><button class="secondary full" id="saveAltitudeLayer">保存高度层（工程设定）</button><div class="scroll-list">'+(layerRows||'<div class="empty-note">尚未定义高度层</div>')+'</div>';
  return shell('02','工作区与环境','框选、重画并保存分析范围。','<div class="button-row"><button class="primary" id="drawWorkspace">框选工作区</button><button class="secondary" id="clearWorkspace">清除</button></div>'+draft+themes+summary+buildingSummary+'<button class="primary full" id="saveWorkspace" '+(!draftWorkspace?'disabled':'')+'>保存工作区范围</button>'+vertical+'<button class="secondary full" id="nextStep" '+(!flow.steps['2']?'disabled':'')+'>下一步：航路设计</button>');
}
export function bind(c){
  c.$('gridOutlineToggle').onchange=e=>c.setGridOutline(e.target.checked);document.querySelectorAll('[name="gridThemeMode"]').forEach(input=>input.onchange=e=>e.target.checked&&c.setGridTheme(e.target.value));
  c.$('drawWorkspace').onclick=c.startWorkspace;c.actionButton('clearWorkspace',c.clearWorkspace);c.actionButton('saveWorkspace',c.saveWorkspace);if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(3);
  c.actionButton('saveAltitudeLayer',()=>c.resourceAction('/api/spatial-3d/altitude-layers',{altitude_layers:[...(c.flow().spatial_3d?.altitude_layers||[]).filter(item=>item.altitude_layer_id!==c.$('altitudeLayerId').value.trim()),{altitude_layer_id:c.$('altitudeLayerId').value.trim(),name:c.$('altitudeLayerId').value.trim(),lower_altitude_m:Number(c.$('altitudeLower').value),upper_altitude_m:Number(c.$('altitudeUpper').value),vertical_reference:c.$('altitudeReference').value,source:'user_configuration',confirmed:true}]}));
}
