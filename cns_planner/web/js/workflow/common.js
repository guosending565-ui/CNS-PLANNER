export const statusText=status=>({not_calculated:'未计算',not_initialized:'未初始化',not_evaluated:'未评价',missing_data:'缺少数据',not_available:'不可用',unknown:'证据不足/尚无法判断',unknown_category:'未知类别',pending_confirmation:'待确认',evidence_required:'需要补充证据',passed:'通过',breach:'净空突破',gap:'缺口',confirmed_gap:'确认缺口',confirmed_deficit:'确认缺口',failed:'失败',not_applicable:'不适用',stale:'已失效',stale_current_project:'对应旧项目状态',current:'当前有效',confirmed:'已确认',applied:'已应用',not_confirmed:'尚未确认',not_applied:'尚未应用',draft:'草稿',recommendation_ready:'需求建议可采用',adopted:'已采用',not_adopted:'尚未采用',no_action_required:'无需规划动作',no_eligible_proposal:'无可行方案',proposal_ready:'提案可审查',ready_for_confirmation:'可确认',objectives_met:'规划目标满足',objectives_not_met:'规划目标未满足',objectives_unknown:'规划目标证据不足',objectives_not_configured:'未配置规划目标',ready:'正常',warning:'警告',error:'错误',no_coverage:'无覆盖',partial_intersection:'部分相交',full_coverage:'完全覆盖'}[status]||status);
export const sourceModeText=mode=>({real:'真实数据',synthetic:'模拟数据',manual:'人工录入'}[mode]||mode||'来源未标明');
export const statusBadge=status=>'<span class="flow-badge flow-'+status+'">'+statusText(status)+'</span>';
// 纯字符串转义：不依赖 DOM，因此在任何环境下行为一致。
export function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,character=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[character]);
}
/**
 * 步骤面板外壳。
 * 标题信息以 data 属性输出，由右侧工作台的固定头部（workbench.js）读取显示，
 * 因此这里不再重复渲染标题；面板内容全部保留在 DOM 中，仅由标签控制显隐。
 */
export const shell=(number,title,text,body)=>'<div class="wb-root" data-workbench-head data-workbench-number="'+number+'" data-workbench-title="'+escapeHtml(title)+'" data-workbench-note="'+escapeHtml(text)+'"></div>'+body;
/** 一级标签面板 / 二级分段面板的快捷构造，转发到 workbench 组件。 */
export {panel as wbPanel,segPanel as wbSegPanel,segmentHint as wbSegHint,section as wbSection,metricCard as wbCard,emptyState as wbEmpty,snapshotLine as wbLine,engineFacts as wbEngine} from './workbench.js';
