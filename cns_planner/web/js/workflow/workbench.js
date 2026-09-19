// =========================================================
// 工作台（右侧当前步骤面板）公共组件与视图状态
//
// 设计要点
//  - 一级标签固定为 [操作] [结果] [高级]，同屏只呈现当前一级标签内容；
//  - 复杂步骤用二级 segmented 再分，同一时刻仍只显示一个子任务；
//  - 面板与分段结构直接从已挂载 DOM 发现：一级面板是根的直属
//    [data-panel-group]，二级分段是该面板内的 [data-seg-name]（名称取自
//    data-seg-label）。新增面板/分段不需要在 main.js 里同步 metadata；
//  - 所有面板始终存在于 DOM 中（便于审计与测试），仅由根节点 data 属性
//    经 CSS 控制显隐，因此切换标签不需要重新渲染业务内容；
//  - 一级/二级点击由本模块自行委托绑定，main.js 不承担 DOM 导航细节；
//  - mutation / renderWorkflow() 之后保持：当前 step、一级标签、二级标签
//    与滚动位置，不会跳回顶部或第一个标签；
//  - 本模块只做展示组织，不读取也不写入任何业务状态。
// =========================================================
// 本模块自带转义，避免与 common.js 形成循环依赖。
function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,character=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[character]);
}

/** 一级标签：id 稳定，作为 CSS 与状态键使用。 */
export const WORKBENCH_TABS=[
  {id:'operate',label:'操作'},
  {id:'result',label:'结果'},
  {id:'advanced',label:'高级'}
];

const TAB_IDS=WORKBENCH_TABS.map(tab=>tab.id);

function normalizeTab(tab){
  return TAB_IDS.includes(tab)?tab:'operate';
}

// ---- 纯标记片段 -------------------------------------------------------------

/**
 * 分区标题：业务语言标题 + 可选状态徽章。
 * 第二个参数只能是状态徽章（statusBadge 的输出）或空字符串——把整块业务正文
 * 当成第二个参数传进来，会让正文落进 .wb-section-head 的 flex 行里，被挤成
 * 竖排文字与超窄表单。需要"标题 + 整块内容"时请使用 block()。
 */
export function section(title,badge=''){
  return '<div class="wb-section-head"><h3>'+escapeHtml(title)+'</h3>'+(badge||'')+'</div>';
}

/**
 * 带正文的分区：标题 + 可选状态徽章 + 正文块。
 * 正文永远在 .wb-section-head 之外，因此不会被 flex 行挤压。
 * @param {string} title 业务语言标题
 * @param {string} body 正文 HTML
 * @param {string} badge 只能是状态徽章或空字符串
 */
export function block(title,body,badge=''){
  return '<section class="wb-section"><div class="wb-section-head"><h3>'+escapeHtml(title)+'</h3>'+(badge||'')+'</div>'+(body||'')+'</section>';
}

/** 指标卡：label / value / 可选注释。 */
export function metricCard(label,value,note=''){
  return '<div class="metric-card"><span class="metric-label">'+escapeHtml(label)+'</span><span class="metric-value">'+escapeHtml(value??'—')+'</span>'+(note?'<span class="metric-note">'+escapeHtml(note)+'</span>':'')+'</div>';
}

/** 指标网格。 */
export function metricGrid(items){
  const values=(items||[]).filter(Boolean);
  if(!values.length)return '';
  return '<div class="metric-grid">'+values.map(item=>'<b>'+escapeHtml(item.value??'—')+'<small>'+escapeHtml(item.label||'')+'</small></b>').join('')+'</div>';
}

/** 空状态：解释为什么没有内容，而不是假装有数据。 */
export function emptyState(text){
  return '<div class="wb-empty">'+escapeHtml(text)+'</div>';
}

/** 业务结论行：第一视觉层用业务语言，tone 只表达状态色。 */
export function snapshotLine(text,tone='',note=''){
  const toneAttr=tone?' data-tone="'+escapeHtml(tone)+'"':'';
  return '<div class="wb-snapshot-line"'+toneAttr+'><b>'+escapeHtml(text)+'</b>'+(note?'<small>'+escapeHtml(note)+'</small>':'')+'</div>';
}

/** 工程内部标识（字段名、算法 id、fingerprint 等）：弱化但仍可查看。 */
export function engineFacts(items){
  const values=(items||[]).filter(Boolean);
  if(!values.length)return '';
  return '<div class="wb-engine">'+values.map(item=>'<span>'+escapeHtml(item)+'</span>').join('')+'</div>';
}

/** 可折叠的工程细节（高级信息仍然可查看）。 */
export function disclosure(summary,body,open=false){
  return '<details class="algorithm-detail"'+(open?' open':'')+'><summary>'+escapeHtml(summary)+'</summary>'+body+'</details>';
}

/** 键值列表。 */
export function definitionList(rows){
  const values=(rows||[]).filter(Boolean);
  if(!values.length)return emptyState('暂无可用信息');
  return '<div class="scroll-list">'+values.map(row=>'<div class="list-row"><span><b>'+escapeHtml(row.label)+'</b>'+(row.note?'<small>'+escapeHtml(row.note)+'</small>':'')+'</span>'+(row.value?'<small>'+escapeHtml(row.value)+'</small>':'')+'</div>').join('')+'</div>';
}

/** 通用表格：columns=[{key,label}]，rows 为对象数组。 */
export function dataTable(columns,rows,empty='暂无数据'){
  const values=rows||[];
  if(!values.length)return emptyState(empty);
  return '<div class="table-wrap"><table><thead><tr>'+columns.map(column=>'<th>'+escapeHtml(column.label)+'</th>').join('')+'</tr></thead><tbody>'
    +values.map(row=>'<tr>'+columns.map(column=>'<td>'+escapeHtml(row[column.key])+'</td>').join('')+'</tr>').join('')
    +'</tbody></table></div>';
}

/**
 * 二级分段提示：同一一级标签下还有哪些子任务，避免"内容去哪了"的困惑。
 * @param {Array<[string,string]>} segments [id,label]
 * @param {string} current 当前分段 id
 */
export function segmentHint(segments,current){
  const list=(segments||[]).filter(item=>item&&item[0]);
  if(list.length<2)return '';
  const others=list.filter(item=>item[0]!==current).map(item=>item[1]);
  if(!others.length)return '';
  return '<div class="wb-hint">本标签下还有：'+escapeHtml(others.join(' · '))+'</div>';
}

/**
 * 二级分段的 HTML 外壳。必须放在一个一级面板内部。
 * 同一个一级标签下同一时刻只显示一个分段；分段 id 需要在整步内唯一。
 *
 * 分段是自描述的：data-seg-name 是稳定 id，data-seg-label 是业务语言名称。
 * 工作台只从已挂载 DOM 顶层的 [data-panel-group] 中发现面板与分段，
 * 因此新增面板/分段不需要在 main.js 里再维护一份重复的 metadata。
 * 第三个参数只能是业务语言名称（label），不承担 className 语义——
 * 之前"像标识符就当 className"的启发式会把 "Legacy / Risk-Aware V2"
 * 这类标签写进 class 属性，导致分段按钮显示错误的名称。
 */
export function segPanel(name,body,label=''){
  const text=label==null?'':String(label).trim();
  return '<div class="wb-seg" data-seg-name="'+escapeHtml(name)+'"'
    +(text?' data-seg-label="'+escapeHtml(text)+'"':'')
    +'>'+(body||'')+'</div>';
}

/**
 * 一级标签面板的 HTML 外壳。
 * @param {string} tab operate|result|advanced
 * @param {string} body 面板内容
 * @param {{segments?:Array<[string,string,string]|{id,label,body}>,segSet?:string,className?:string,none?:string}} options
 *   segments 提供 [id,label,body] 或 {id,label,body} 时自动生成唯一的二级分段容器（同一时刻只显示一个）；
 *   segSet 为空表示该面板没有二级分段，内容始终可见；
 *   none 提供时作为没有任何二级分段声明时的回退内容（用于只声明了分段标题的步骤）。
 */
export function panel(tab,body,{segments,segSet,className,none}={}){
  const group=normalizeTab(tab);
  const list=(segments||[]).map(item=>Array.isArray(item)?{id:item[0],label:item[1],body:item[2]||''}:item).filter(item=>item&&item.id);
  if(!list.length){
    return '<div class="wb-panel '+(className||'')+'" data-panel-group="'+group+'" data-seg-set="none">'
      +(body||none||'')+'</div>';
  }
  const set=segSet||group;
  return '<div class="wb-panel '+(className||'')+'" data-panel-group="'+group+'" data-seg-set="'+escapeHtml(set)+'"'
    +' data-seg-ids="'+escapeHtml(list.map(item=>item.id).join(' '))+'"'
    +' data-seg-labels="'+escapeHtml(list.map(item=>item.id+':'+(item.label||item.id)).join(' '))+'">'
    +list.map(item=>segPanel(item.id,item.body||'',item.label||item.id)).join('')
    +'</div>';
}

// ---- 视图控制器 -------------------------------------------------------------

function button(className,label,dataset){
  const node=document.createElement('button');
  node.type='button';
  node.className=className||'';
  node.textContent=label;
  for(const [key,value] of Object.entries(dataset||{}))node.dataset[key]=value;
  return node;
}

/**
 * 工作台视图控制器。
 * @param {{getState:Function,setState:Function}} options
 */
export function createWorkbench({getState,setState}={}){
  let currentStep=null,mountedRoot=null;

  const readState=()=>{
    const value=(getState&&getState())||{};
    return {
      step:Number(value.step)||1,
      tab:normalizeTab(value.tab),
      segs:{...(value.segs||{})},
      scroll:Number(value.scroll)||0
    };
  };

  const writeState=patch=>{
    if(setState)setState({...readState(),...patch});
  };

  // ---- 从已挂载 DOM 发现一级面板与二级分段 --------------------------------
  // 真实步骤模块只输出渲染结果，不导出 panels/segments metadata。
  // 工作台因此直接从挂载根的直属 [data-panel-group] 读取结构：
  // 面板 = 根的第一层子节点；分段 = 当前面板的真实子节点 [data-seg-name]。
  // 新增 panel / segment 不需要在 main.js 里维护重复 metadata。

  /** 一级面板所属的容器：沿父链上溯到第一个"不是面板"的祖先。 */
  function panelOwner(node,root){
    for(let current=node.parentNode;current;current=current.parentNode){
      if(current===root)return root;
      if(current.dataset&&current.dataset.panelGroup)continue;
      return null;
    }
    return null;
  }

  const panelNodes=()=>{
    const root=mountedRoot||document.getElementById('workbenchPanel');
    if(!root||typeof root.querySelectorAll!=='function')return [];
    const values=[];
    for(const node of root.querySelectorAll('[data-panel-group]')){
      // 只认工作台根下第一层的一级面板：嵌套面板属于面板内部结构，不参与一级标签
      if(panelOwner(node,root)!==root)continue;
      const group=normalizeTab(node.dataset?node.dataset.panelGroup:'');
      if(!values.some(item=>item.group===group))values.push({group,node});
    }
    return values;
  };

  const panels=()=>panelNodes().map(item=>item.group);

  /** 解析 panel 声明的 [id:label] 列表（面板内部 DOM 缺失时的回退）。 */
  function declaredLabels(panelNode){
    const map=new Map(),declared=String(panelNode.dataset?.segLabels||'').trim();
    for(const entry of declared?declared.split(/\s+/):[]){
      const index=entry.indexOf(':');
      if(index>0)map.set(entry.slice(0,index),entry.slice(index+1));
    }
    return map;
  }

  /** 该一级标签下真实的二级分段（按 DOM 顺序，标签来自自描述属性）。 */
  const segmentsFor=tab=>{
    const found=panelNodes().find(item=>item.group===normalizeTab(tab));
    if(!found)return [];
    const fallback=declaredLabels(found.node),list=[];
    for(const node of found.node.querySelectorAll('[data-seg-name]')){
      const id=String(node.dataset.segName||'');
      if(!id)continue;
      list.push({id,label:String(node.dataset.segLabel||'')||fallback.get(id)||id});
    }
    if(list.length)return list;
    return String(found.node.dataset.segIds||'').split(/\s+/).filter(Boolean)
      .map(id=>({id,label:fallback.get(id)||id}));
  };

  // 当前应生效的二级分段：只认当前一级标签下真实存在的分段，否则回退到第一个
  const activeSeg=state=>{
    const list=segmentsFor(state.tab);
    if(!list.length)return '';
    const stored=state.segs[state.tab];
    return list.some(segment=>segment.id===stored)?stored:list[0].id;
  };

  // ---- 导航事件：由工作台自己绑定，不重复注册，不依赖 main.js ---------------
  // 一次委托同时覆盖 #workbenchTabs（一级）与 #workbenchSegs（二级）；
  // 导航只重绘导航与显隐属性，不重新渲染业务内容，因此不丢草稿与状态。
  function activate(target){
    if(!target||!target.dataset)return;
    if(target.dataset.wbTab)return {tab:target.dataset.wbTab};
    if(target.dataset.wbSeg)return {seg:target.dataset.wbSeg,segSet:target.dataset.wbSegSet};
    return null;
  }

  function onClick(event){
    const action=activate(event&&event.target);
    if(action)controller.navigate(action);
  }

  let navigationBound=false;
  function bindNavigation(){
    if(navigationBound)return;
    navigationBound=true;
    document.addEventListener('click',onClick);
  }

  const controller={
    clearState(){
      writeState({tab:'operate',segs:{},scroll:0});
    },

    body(){return document.getElementById('workbenchBody');},

    applyView(){
      const root=(mountedRoot&&mountedRoot.isConnected!==false)?mountedRoot:document.getElementById('workbenchPanel');
      if(root&&currentStep){
        const state=readState();
        root.dataset.tab=state.tab;
        const seg=activeSeg(state);
        if(seg)root.dataset.seg=seg;else delete root.dataset.seg;
      }
    },

    /** 一级标签：只呈现当前步骤真实存在的一级面板，按钮直接填进 #workbenchTabs。 */
    tabs(){
      const container=document.getElementById('workbenchTabs');
      if(!container)return;
      const state=readState();
      const available=new Set(panels());
      const nodes=[];
      for(const tab of WORKBENCH_TABS){
        if(!available.has(tab.id))continue;
        nodes.push(button(tab.id===state.tab?'tab-active':'',tab.label,{'wbTab':tab.id}));
      }
      container.replaceChildren(...nodes);
    },

    /** 二级分段：名称来自分段自身的 data-seg-label，不在外部重复维护。 */
    segs(){
      const container=document.getElementById('workbenchSegs');
      if(!container)return;
      const state=readState();
      const list=segmentsFor(state.tab);
      container.replaceChildren();
      container.hidden=!list.length;
      if(!list.length)return;
      const active=activeSeg(state);
      const host=document.createElement('div');
      host.className='segmented';
      host.dataset.segSet=state.tab;
      for(const segment of list){
        host.append(button(segment.id===active?'seg-active':'',segment.label,{'wbSeg':segment.id,'wbSegSet':state.tab}));
      }
      container.append(host);
    },

    /** 更新步骤标题区：优先使用面板自带的标题，其次用步骤声明。 */
    head(){
      const step=currentStep||{};
      const root=mountedRoot||document.getElementById('workbenchPanel')||document.getElementById('workflowPanel');
      const host=root?root.querySelector('[data-workbench-head]'):null;
      const number=document.getElementById('workbenchStepNumber');
      const title=document.getElementById('workbenchStepTitle');
      const note=document.getElementById('workbenchStepNote');
      const panelNumber=host?host.dataset.workbenchNumber:'';
      const panelTitle=host?host.dataset.workbenchTitle:'';
      const panelNote=host?host.dataset.workbenchNote:'';
      if(number)number.textContent=String(panelNumber||step.number||'').padStart(2,'0');
      if(title)title.textContent=panelTitle||step.title||'';
      if(note)note.textContent=panelNote||step.note||'';
      // 只移除面板自带的标题占位；wb-root 与全部业务内容必须留在 DOM 中
      if(host&&host.parentNode)host.parentNode.removeChild(host);
    },

    /** 只重绘导航（标题、一级标签、二级分段）与显隐属性。 */
    renderNavigation(){
      this.head();
      this.tabs();
      this.segs();
      this.applyView();
    },

    /**
     * 注入渲染好的面板内容并恢复导航状态。
     * @param {{root:HTMLElement,step:object}} options
     */
    mount({root,step}={}){
      currentStep=step||currentStep;
      const host=document.getElementById('workflowPanel');
      if(root){
        root.classList.add('wb-root');
        // 稳定 id：事件委托与外部查询都通过它取回面板根节点
        if(!root.id)root.id='workbenchPanel';
        mountedRoot=root;
      }
      if(host&&root)host.replaceChildren(root);
      // 无论重新渲染多少次，导航监听只注册一次
      bindNavigation();
      this.renderNavigation();
      return controller;
    },

    /**
     * 一次标签点击：写状态、重绘导航、更新显隐。
     * 不重新渲染业务内容，因此表单草稿与列表状态保持不变。
     */
    navigate({tab,seg,segSet}={}){
      const state=readState();
      const patch={};
      if(tab&&TAB_IDS.includes(tab))patch.tab=tab;
      if(seg){
        const set=segSet||(patch.tab||state.tab);
        patch.segs={...state.segs,[set]:seg};
      }
      writeState(patch);
      this.renderNavigation();
      const body=this.body();
      if(body){
        // 切换标签后内容高度变化，滚动位置按容器实际范围收敛，不强制回到顶部
        const state2=readState();
        body.scrollTop=Math.min(state2.scroll,Math.max(0,body.scrollHeight-body.clientHeight));
      }
      return readState();
    },

    state(){return readState();},
    step(){return currentStep;}
  };

  return controller;
}
