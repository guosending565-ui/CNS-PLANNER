export const statusText=status=>({not_calculated:'未计算',missing_data:'缺少数据',not_available:'不可用',unknown_category:'未知类别',pending_confirmation:'待确认',passed:'通过',gap:'缺口',failed:'失败',not_applicable:'不适用',stale:'已失效',ready:'正常',warning:'警告',error:'错误',no_coverage:'无覆盖',partial_intersection:'部分相交',full_coverage:'完全覆盖'}[status]||status);
export const statusBadge=status=>'<span class="flow-badge flow-'+status+'">'+statusText(status)+'</span>';
export function escapeHtml(value){const div=document.createElement('div');div.textContent=String(value??'');return div.innerHTML;}
export const shell=(number,title,text,body)=>'<div class="section-label">'+number+' / 六步业务流程</div><h2>'+title+'</h2><p>'+text+'</p>'+body+'<div id="panelError" class="inline-error"></div>';
