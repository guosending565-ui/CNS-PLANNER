import {escapeHtml,statusBadge} from './common.js';

const ORDER=['detect','track','processing','decision','communication','aircraft_reaction'];
const COLORS=['#2563a6','#3b82b4','#50a0a8','#6cab87','#d19645','#c45454'];
const fmt=value=>Number.isFinite(value)?Number(value).toFixed(2):'—';

export function protectionBudgetModel(result={}){
  const parts=ORDER.map((name,index)=>({name,value_s:result.components?.[name]?.value_s,color:COLORS[index]}));
  return {status:result.status||'not_calculated',parts,total_s:result.t_pre_s,d_reaction_m:result.d_reaction_m,maneuver_distance_m:result.maneuver_distance_m,uncertainty_distance_m:result.uncertainty_distance_m,d_protect_m:result.d_protect_m};
}

export function renderProtectionBudget(result={}){
  const model=protectionBudgetModel(result),known=model.parts.filter(item=>Number.isFinite(item.value_s)),total=known.reduce((sum,item)=>sum+item.value_s,0);
  const stack=known.length?known.map(item=>'<span title="'+escapeHtml(item.name)+' '+fmt(item.value_s)+' s" style="display:inline-flex;justify-content:center;min-width:24px;width:'+Math.max(4,item.value_s/Math.max(total,.0001)*100)+'%;background:'+item.color+';color:white;padding:6px 2px;font-size:10px">'+escapeHtml(item.name)+'</span>').join(''):'<span style="padding:8px">时间分量未完整确认</span>';
  const legend=model.parts.map(item=>'<span style="white-space:nowrap"><i style="display:inline-block;width:9px;height:9px;background:'+item.color+'"></i> '+escapeHtml(item.name)+' '+fmt(item.value_s)+' s</span>').join(' · ');
  return '<h3>Protection Budget 可视化 V1 '+statusBadge(model.status)+'</h3><div class="parameter-note"><strong>engineering protection budget / regulatory well-clear not evaluated</strong>。EncounterScenario 没有空间位置与 heading，d_protect 禁止解释或绘制为航路 buffer、保护圆或法规 well-clear 区域。</div><button class="secondary full" id="evaluateProtectionBudget">计算/刷新工程预算</button><div data-protection-stack style="display:flex;width:100%;overflow:hidden;border-radius:4px;background:#e7ecea">'+stack+'</div><div class="flow-summary">'+legend+'<br>t_pre '+fmt(model.total_s)+' s · d_reaction '+fmt(model.d_reaction_m)+' m · maneuver_distance '+fmt(model.maneuver_distance_m)+' m · uncertainty_distance '+fmt(model.uncertainty_distance_m)+' m · <strong>d_protect '+fmt(model.d_protect_m)+' m</strong>'+(model.status==='unknown'?'<br>'+escapeHtml((result.reasons||[]).join('；')):'')+'</div>';
}
