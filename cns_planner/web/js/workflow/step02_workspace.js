import {escapeHtml,shell,statusText} from './common.js';
export function render({flow,draftWorkspace,gridDisplay,populationDisplayLabel,formatNumber}){
  const workspace=flow.workspace,health=workspace?.health,grid=flow.grid,attributes=flow.grid_attributes||{},risk=flow.grid_risk||{};
  let summary='<div class="empty-note">尚未保存工作区。点击“框选工作区”后在地图拖出矩形。</div>';
  if(workspace)summary='<div class="metric-grid"><b>'+workspace.area_km2.toLocaleString()+' km²<small>工作区面积</small></b><b>'+statusText(health.population.status)+'<small>人口数据</small></b><b>'+statusText(health.airspace.status)+'<small>空域数据</small></b><b>'+health.loaded_layer_count+'<small>已加载图层</small></b></div><div class="missing-list">DEM：'+statusText(health.terrain?.status||'missing_data')+' · 建筑：missing_data · 财产：missing_data</div><div class="flow-summary">'+escapeHtml(health.terrain?.message||'DEM 状态未知')+'<br>'+(grid?.status==='passed'?'MH/T 4063.1 标准网格：L'+grid.level+' · '+grid.count+' 格'+(grid.coarsened?'（已按数量限制降级）':''):'标准网格：未生成')+'<br>人口映射：'+statusText(attributes.population?.status||'not_calculated')+' · 已处理 '+(attributes.population?.count||0)+' 格（有效 '+(attributes.population?.covered_count||0)+' 格）<br>DEM 映射：'+statusText(attributes.terrain?.status||'not_calculated')+' · 已处理 '+(attributes.terrain?.count||0)+' 格（有效 '+(attributes.terrain?.covered_count||0)+' 格）<br>空域映射：'+statusText(attributes.airspace?.status||'not_calculated')+' · 已处理 '+(attributes.airspace?.count||0)+' 格（命中 '+(attributes.airspace?.hit_count||0)+' 格）<br>交通暴露：'+statusText(attributes.traffic?.status||'not_calculated')+' · 已处理 '+(attributes.traffic?.count||0)+' 格（命中 '+(attributes.traffic?.covered_count||0)+' 格）<br>冲突暴露：'+statusText(attributes.conflict?.status||'not_calculated')+' · 已处理 '+(attributes.conflict?.count||0)+' 格（命中 '+(attributes.conflict?.covered_count||0)+' 格）<br>相对风险：'+statusText(risk.status||'not_calculated')+' · 完整度 '+formatNumber((risk.data_completeness||0)*100)+'%</div>';
  const checked=value=>gridDisplay.theme===value?'checked':'';
  const ids={none:'gridThemeNone',population:'gridPopulationTheme',terrain:'gridTerrainTheme'};
const themeOptions = [
  ['none', '无'],
  ['population', '人口密度'],
  ['terrain', '地形 DEM'],
  ['traffic_exposure', '交通暴露'],
  ['conflict_exposure', '冲突暴露'],
  ['ground_risk', '地面风险'],
  ['airspace_risk', '空域约束风险'],
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
          <input
            type="radio"
            name="gridThemeMode"
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
  return shell('02','工作区与环境','框选、重画并保存分析范围。','<div class="button-row"><button class="primary" id="drawWorkspace">框选工作区</button><button class="secondary" id="clearWorkspace">清除</button></div>'+draft+themes+summary+'<button class="primary full" id="saveWorkspace" '+(!draftWorkspace?'disabled':'')+'>保存工作区范围</button><button class="secondary full" id="nextStep" '+(!flow.steps['2']?'disabled':'')+'>下一步：航路设计</button>');
}
export function bind(c){
  c.$('gridOutlineToggle').onchange=e=>c.setGridOutline(e.target.checked);document.querySelectorAll('[name="gridThemeMode"]').forEach(input=>input.onchange=e=>e.target.checked&&c.setGridTheme(e.target.value));
  c.$('drawWorkspace').onclick=c.startWorkspace;c.actionButton('clearWorkspace',c.clearWorkspace);c.actionButton('saveWorkspace',c.saveWorkspace);if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(3);
}
