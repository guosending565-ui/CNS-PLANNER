// =========================================================
// 步骤装配：把六个步骤模块的 render 输出挂载为右侧工作台面板。
//
// 各步骤 render 仍然返回完整 HTML 字符串（包含标题、一级标签、
// 二级分段与全部面板内容），因此：
//  - 既有审计与测试可以直接检查返回的 HTML；
//  - 工作台只负责把这段 HTML 放进 DOM，并按导航状态控制显隐；
//  - 面板内容始终挂载，切换标签不重新渲染，表单与列表状态不丢失。
// =========================================================

// 单根容器：shell() 返回的根节点就是 [data-workbench-head] 的父节点
function toFragment(html){
  const template=document.createElement('template');
  template.innerHTML=String(html||'').trim();
  return template.content;
}

/** 找到面板根节点（.wb-root），并保证它在 workflowPanel 内是唯一子元素。 */
function normalizeHost(host){
  if(!host)return null;
  const root=host.querySelector?host.querySelector('.wb-root'):null;
  if(root&&root.parentNode===host&&host.children.length>1)host.replaceChildren(root);
  return root||host.firstElementChild||null;
}

/**
 * 渲染一个步骤为工作台面板并挂载。
 * @param {{step:object,index:number,context:object}} input
 * @returns {HTMLElement|null} 挂载后的面板根节点
 */
export function renderWorkflowSteps({step,context}){
  const host=document.getElementById('workflowPanel');
  if(!host||!step||typeof step.render!=='function')return null;
  const html=step.render(context);
  host.replaceChildren(toFragment(html));
  return normalizeHost(host);
}
