// =========================================================
// 壳层交互与地图命中测试
//
// 把 DOM 层的绑定从 main.js 抽出来，使入口文件只保留装配职责。
// 这里不做任何业务判断，只把用户动作转成回调。
// =========================================================

/** 图层开关当前值（全部来自图层抽屉，默认开启）。 */
export function layerSwitches($,ids){
  const value={};
  for(const id of ids)value[id]=$(id)?.checked!==false;
  return value;
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
