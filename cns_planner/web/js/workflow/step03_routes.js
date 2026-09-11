import {escapeHtml,shell,statusBadge,statusText} from './common.js';
export function render({flow,interactionMode}){
  const nodes=(flow.nodes||[]).map(node=>'<div class="list-row"><span><b>'+node.node_id+'</b> '+escapeHtml(node.name)+'<small>'+node.coordinate.map(value=>value.toFixed(5)).join(', ')+'</small></span><button data-delete-node="'+node.node_id+'">×</button></div>').join('');
  const routes=(flow.scenario_routes||[]).map(route=>{const result=(flow.operational_routes||[]).find(item=>item.route_id===route.route_id);return '<div class="list-row route-row"><span><b>'+route.route_id+'</b> '+route.direction+' '+statusBadge(result?.status||'not_calculated')+'</span><button data-delete-route="'+route.route_id+'">×</button></div>';}).join('');
  const body='<button class="'+(interactionMode==='node'?'primary':'secondary')+' full" id="addNodeMode">地图点击增加起降点</button><div class="scroll-list">'+(nodes||'<div class="empty-note">至少添加两个点</div>')+'</div><label>生成方向</label><select id="routeDirection"><option value="both">双向（独立生成两个 route_id）</option><option value="ab">A→B</option><option value="ba">B→A</option></select><div class="button-row"><button class="secondary" id="scenarioRoutes">生成场景航路</button><button class="primary" id="operationalRoutes">生成运行航路</button></div><div class="scroll-list route-list">'+(routes||'<div class="empty-note">尚无航路</div>')+'</div><div class="flow-summary">已退役编号：'+(flow.retired_route_ids.join(', ')||'无')+'<br>环境风险：'+statusText(flow.risks.environment.status)+'</div><button class="primary full" id="nextStep" '+(!flow.steps['3']?'disabled':'')+'>下一步：运行规则</button>';
  return shell('03','航路设计','地图点击增加起降点；场景与运行航路分别保存。',body);
}
export function bind(c){
  c.$('addNodeMode').onclick=c.toggleNodeMode;c.actionButton('scenarioRoutes',()=>c.mutate('scenario',{direction:c.$('routeDirection').value}));c.actionButton('operationalRoutes',()=>c.mutate('operational'));
  document.querySelectorAll('[data-delete-node]').forEach(button=>button.onclick=()=>c.mutate('node-delete',{node_id:button.dataset.deleteNode}).catch(error=>c.panelError(error.message)));
  document.querySelectorAll('[data-delete-route]').forEach(button=>button.onclick=()=>c.mutate('route-delete',{route_id:button.dataset.deleteRoute}).catch(error=>c.panelError(error.message)));
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(4);
}
