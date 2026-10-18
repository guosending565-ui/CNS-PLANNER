// =========================================================
// 壳层交互与地图命中测试
//
// 把 DOM 层的绑定从 main.js 抽出来，使入口文件只保留装配职责。
// 这里不做任何业务判断，只把用户动作转成回调。
// =========================================================

/**
 * 图层开关当前值（全部来自图层抽屉，默认开启）。
 *
 * key 统一就是图层抽屉里的 checkbox id 本身（例如 ``buildingClearanceLayer`` /
 * ``v3CandidateLayer`` / ``layeredFeasibilityLayer`` / ``layeredCandidateLayer`` /
 * ``existingCnsLayer`` / ``candidateSiteLayer``），绘制侧
 * （map/display_layers.js）与 LAYER_IDS 使用同一把 key，不再出现两套命名。
 */
export function layerSwitches($,ids){
  const value={};
  for(const id of ids)value[id]=$(id)?.checked!==false;
  return value;
}

/** 人口 / 地形原始栅格的图例跟随各自开关。 */
export function updateRasterLegends($){
  if($('populationRasterLegend'))$('populationRasterLegend').hidden=!$('pop')?.checked;
  if($('terrainRasterLegend'))$('terrainRasterLegend').hidden=!$('terrain')?.checked;
}

/**
 * 高度层障碍的三态开关。
 *
 * BUG-R37A-002：这三个 id **同时**出现在 ``LAYER_IDS`` 里，因此曾经被两处
 * ``onchange`` 赋值：先是下面的专用 handler（``onConstraintLayer``，会先按需补齐
 * 网格几何再读约束场），随后又被通用开关循环覆盖成"只重绘"。
 * 结果是勾选「高度层障碍」既不发 ``GET /api/workspace/grid``，也不读约束场，
 * 图层静默画不出来。二者必须互斥：专用 handler 只能有一份。
 */
const CONSTRAINT_LAYER_IDS=[
  'altitudeConstraintLayer','altitudeConstraintUnknownLayer','altitudeConstraintPassLayer'
];

/**
 * 绑定图层抽屉的全部控件（基础栅格、透明度、统一 layer switches）。
 * 全部使用 onchange/oninput 赋值，重复调用也不会叠加监听器。
 * @param {{$,layerIds,queue,paint,setGridOutline,updateGridNotice,updateGridThemeLegend,onOnlineTiles,updateMapLegend,onConstraintLayer,onGridLayer}} options
 */
export function bindLayerControls({
  $,layerIds,queue,paint,setGridOutline,updateGridNotice,updateGridThemeLegend,onOnlineTiles,updateMapLegend,onConstraintLayer,onGridLayer
}){
  // 基础开关：air 只重绘；pop/terrain 同时刷新栅格图例
  if($('air'))$('air').onchange=queue;
  if($('pop'))$('pop').onchange=()=>{updateRasterLegends($);queue();};
  if($('terrain'))$('terrain').onchange=()=>{updateRasterLegends($);queue();};
  if($('online'))$('online').onchange=()=>{onOnlineTiles();paint();};
  updateRasterLegends($);
  if($('opacity'))$('opacity').oninput=()=>{$('opacityValue').textContent=$('opacity').value+'%';queue();};
  if($('terrainOpacity'))$('terrainOpacity').oninput=()=>{$('terrainOpacityValue').textContent=$('terrainOpacity').value+'%';queue();};
  // 高度层障碍：勾选后按需读取逐格明细（**默认关闭**，不参与启动加载）。
  // BUG-R37A-002：这一份是**唯一**的约束层 handler；下面的通用循环必须跳过这三个 id。
  for(const id of CONSTRAINT_LAYER_IDS){
    if($(id))$(id).onchange=()=>{onConstraintLayer?.();updateGridThemeLegend();updateMapLegend?.();paint();};
  }
  // layerIds 是统一开关集合（含 referenceRoutePointLayer），这里再补 gridLayer
  for(const id of [...layerIds,'gridLayer']){
    if(CONSTRAINT_LAYER_IDS.includes(id))continue;
    const input=$(id);if(!input)continue;
    input.onchange=()=>{
      if(id==='gridLayer'){
        setGridOutline($('gridLayer').checked);
        if($('gridOutlineToggle'))$('gridOutlineToggle').checked=$('gridLayer').checked;
        // BUG-R37A-002：勾选「标准规划网格」是"首次真正需要网格几何"的产品入口，
        // 必须在这里按需 hydrate 逐格几何（专题 / 边界 / 点击 popup 都依赖它）。
        onGridLayer?.();
      }
      updateGridNotice();updateGridThemeLegend();updateMapLegend?.();paint();
    };
  }
}

/**
 * 绑定图层抽屉、图例收起、左右栏折叠与顶部导出菜单。
 * @param {{$,downloadExport,previewReport,generateReport,downloadReport,saveProject,panelError}} options
 */
export function bindShell(options){
  const {$,downloadExport,previewReport,generateReport,downloadReport,saveProject,panelError}=options;
  const drawer=$('layerDrawer'),drawerToggle=$('layerDrawerToggle');
  if(drawer&&drawerToggle)drawerToggle.onclick=()=>{
    const open=drawer.dataset.open!=='true';
    drawer.dataset.open=String(open);
    drawerToggle.setAttribute('aria-expanded',String(open));
    const caret=drawerToggle.querySelector('.drawer-caret');
    if(caret)caret.textContent=open?'收起':'展开';
  };
  const legend=$('legend'),legendToggle=$('legendToggle');
  if(legend&&legendToggle)legendToggle.onclick=()=>legend.classList.toggle('legend-collapsed');
  const shell=$('appShell');
  if($('railToggle'))$('railToggle').onclick=event=>{
    const collapsed=shell.classList.toggle('rail-collapsed');
    event.currentTarget.textContent=collapsed?'展开':'收起';
  };
  if($('workbenchToggle'))$('workbenchToggle').onclick=event=>{
    const collapsed=shell.classList.toggle('workbench-collapsed');
    shell.classList.remove('workbench-open');
    event.currentTarget.textContent=collapsed?'展开':'收起';
  };
  // 窄屏：工作台以浮层展开，不压缩地图
  if($('workbenchOpen'))$('workbenchOpen').onclick=()=>{
    const open=shell.classList.toggle('workbench-open');
    shell.classList.toggle('workbench-collapsed',!open);
    const toggle=$('workbenchToggle');
    if(toggle)toggle.textContent=open?'收起':'展开';
  };
  // 窄屏（约 1100px 级）默认折叠右侧工作台，保证地图仍可用
  if(window.matchMedia('(max-width:1180px)').matches)shell.classList.add('workbench-collapsed');
  const menuButton=$('exportMenuBtn'),menuPanel=$('exportMenuPanel');
  if(!menuButton||!menuPanel)return;
  menuButton.onclick=event=>{
    event.stopPropagation();
    const open=menuPanel.hidden;
    menuPanel.hidden=!open;
    menuButton.setAttribute('aria-expanded',String(open));
  };
  document.addEventListener('click',event=>{
    if(menuPanel.hidden)return;
    if(menuPanel.contains(event.target)||menuButton.contains(event.target))return;
    menuPanel.hidden=true;menuButton.setAttribute('aria-expanded','false');
  });
  menuPanel.addEventListener('click',async event=>{
    const exportButton=event.target.closest('[data-export-kind]');
    const reportButton=event.target.closest('[data-report-action]');
    menuPanel.hidden=true;menuButton.setAttribute('aria-expanded','false');
    try{
      if(exportButton)await downloadExport(exportButton.dataset.exportKind);
      else if(reportButton){
        const action=reportButton.dataset.reportAction;
        if(action==='preview')await previewReport();
        else if(action==='generate')await generateReport();
        else await downloadReport('package');
      }
    }catch(exc){panelError(exc.message);}
  });
  $('saveProjectTop').onclick=()=>saveProject();
  // A7-UI-01：二维主界面的三维入口。**只**在新标签打开既有 `/visualization-3d.html`：
  // 不在本页新建 Viewer 实例、不传任何参数、不发任何写请求、不触发规划计算。
  // 三维页自己通过只读接口读取"当前项目"，因此与二维页共用同一个当前项目。
  const viewer3dButton=$('openViewer3d');
  if(viewer3dButton)viewer3dButton.onclick=()=>{
    const opened=window.open('/visualization-3d.html','_blank');
    // 弹窗被拦截时给出可读结论，绝不静默什么都不发生；二维地图状态保持不变。
    if(!opened){
      panelError('三维 Viewer 未能打开（浏览器拦截了新标签）；请允许本站弹出窗口后重试。');
      return;
    }
    // 反向断言式安全：新页面不得通过 window.opener 反向操作二维页。
    try{opened.opener=null;}catch(_){/* 跨源/已关闭时无需处理 */}
    try{opened.focus();}catch(_){/* 聚焦失败不影响新标签已经打开这一事实 */}
  };
  $('settingsBtn').onclick=()=>$('settings').showModal();
  $('connection').onclick=()=>$('settings').showModal();
}

/** 图表角落的 LOD 角标：只陈述当前显示层级与聚合数量。 */
export function updateLodBadge(target,plan,escapeHtml){
  if(!target||!plan)return;
  const clustered=plan.clusterCounts.nodes+plan.clusterCounts.sites;
  target.innerHTML='显示层级 <b>'+escapeHtml(plan.levelLabel)+'</b> · '+escapeHtml(plan.resolution)
    +(clustered?' · 已聚合 '+clustered+' 点':'');
}

/** 左侧六步导航的当前/已完成/待处理状态（只读 flow.steps）。 */
export function renderRailSteps($,flow,currentStep,query){
  const label={active:'进行中',done:'已完成',todo:'待处理'};
  for(const button of query('[data-step]')){
    const step=Number(button.dataset.step);
    const done=flow?.steps?.[String(step)]===true;
    const stateName=step===currentStep?'active':(done?'done':'todo');
    button.classList.toggle('active',step===currentStep);
    button.dataset.state=stateName;
    const target=button.querySelector('.rail-state');
    if(target&&target.lastChild)target.lastChild.textContent=label[stateName];
  }
}
