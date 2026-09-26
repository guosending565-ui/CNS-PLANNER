import {escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock,wbEmpty,wbLine,wbEngine,wbCard,
  inputRequirementText,inputRequirementBadge,sourceStateText,workflowStatusText,emptyReasonText,
  advancedAuditNote,blockerList,primaryAction,nextStepBar} from './common.js';

export function algorithmSelectionKey(item){return [item?.algorithm_type,item?.algorithm_id,item?.version].join('|');}
export function algorithmManifestDetails(item){return item?{
  identity:item.algorithm_id+'@'+item.version,provider:item.provider,maturity:item.maturity,
  inputs:(item.inputs||[]).join(', '),outputs:(item.outputs||[]).join(', '),
  assumptions:(item.assumptions||[]).join('；')||'无',limitations:(item.limitations||[]).join('；')||'无',
  references:(item.references||[]).join('；')||'未登记'
}:null;}

/**
 * 必要输入清单（B4X §7）。
 *
 * 需求等级语义（与第 02 / 04 步共用同一份集中映射）：
 *  - `required`  必需：缺它就不能继续；
 *  - `assumable` 可采用工程假设：缺它仍可继续，但必须由工程假设替代，并且结果会带提示；
 *  - `optional`  可选：缺它只是少一个视角，不阻塞；
 *  - `enhanced`  增强数据：有了更精细，没有不影响结论有效性。
 *
 * 每一项只声明**需求等级 + 当前事实状态**，不描述算法实现细节。
 */
export const INPUT_REQUIREMENTS=[
  {id:'population',label:'人口',level:'assumable',
    note:'WorldPop 源像元人口数；海上 / 无人区的无数据语义必须显式确认。'},
  {id:'terrain',label:'地形',level:'required',
    note:'GLO-30 DSM 与 FABDEM DTM；建筑地面高程与地形净空的事实来源。'},
  {id:'buildings',label:'建筑',level:'required',
    note:'GBA LoD1 单体与建筑环境网格；建筑三维净空与约束场的建筑域事实。'},
  {id:'towers',label:'铁塔',level:'assumable',
    note:'真实通信铁塔站址；塔顶高程需确认后才参与障碍判定。'},
  {id:'airspace',label:'空域 / 要地',level:'assumable',
    note:'禁飞 / 受限区域与保护要地的确认几何；缺少时保持证据不足。'},
  {id:'devices',label:'设备 / 设施',level:'assumable',
    note:'设备资料库与既有 CNS 设施；能力口径必须显式声明。'},
  {id:'landing_sites',label:'起降点 / 参考航线',level:'optional',
    note:'参考起降点与真实参考航线；只读参考层，不自动进入项目航路。'},
  {id:'land_mask',label:'陆域掩膜',level:'enhanced',
    note:'雷达监视规划的海域判定来源；缺少时地表类别保持未知。'}
];

/**
 * 读取某个输入项在 flow / state 中的事实状态。
 *
 * 只读既有字段，不推断、不补默认值；拿不到事实时如实返回"尚未配置"。
 */
export function inputRequirementStatus(item,{state,flow}={}){
  const paths=(state&&state.paths)||{};
  const health=(state&&state.data_health)||{};
  const healthItems=Array.isArray(health.sources)?health.sources:[];
  const findHealth=role=>healthItems.find(entry=>String(entry.role||entry.id||'')===role)||null;
  const gridAttributes=(flow&&flow.grid_attributes)||{};
  const workspaceHealth=((flow&&flow.workspace)||{}).health||{};
  const table={
    population:{configured:Boolean(paths.population),health:findHealth('population')},
    terrain:{configured:Boolean(paths.terrain)||Boolean(paths.terrain_dtm),health:findHealth('terrain')},
    buildings:{configured:Boolean(paths.buildings)||Boolean(paths.building_grid),health:findHealth('buildings')},
    towers:{configured:Boolean(paths.towers),health:findHealth('towers')},
    airspace:{configured:Boolean(paths.basemap)&&Boolean(gridAttributes.airspace),health:findHealth('airspace')},
    devices:{configured:Boolean(flow&&((flow.device_catalog||{}).count||(flow.existing_cns_facilities||{}).count)),health:findHealth('devices')},
    landing_sites:{configured:Boolean(paths.reference_landing_sites)||Boolean(paths.reference_routes),health:findHealth('reference_landing_sites')},
    land_mask:{configured:Boolean(paths.land_mask),health:findHealth('land_mask')}
  };
  const entry=table[item.id]||{configured:false,health:null};
  const status=entry.health?(entry.health.status||''):'';
  const mapped=(gridAttributes[item.id]||{}).status||workspaceHealth[item.id]?.status||'';
  return {
    configured:entry.configured,
    status:status||'',
    mapped:mapped||'',
    label:!entry.configured?sourceStateText('not_configured'):statusText(status||'ready'),
    reason:entry.health?(entry.health.detail||entry.health.message||''):''
  };
}

/** 必要输入清单的只读展示：按需求等级分组，不展示算法实现细节。 */
export function inputRequirementPanel({state,flow}){
  const groups=['required','assumable','optional','enhanced'];
  const rows=groups.map(level=>{
    const items=INPUT_REQUIREMENTS.filter(item=>item.level===level);
    if(!items.length)return '';
    return '<div class="input-requirement-group" data-level="'+level+'">'
      +'<div class="input-requirement-head">'+inputRequirementBadge(level)
      +'<small>'+items.length+' 项</small></div>'
      +items.map(item=>{
        const status=inputRequirementStatus(item,{state,flow});
        return '<div class="list-row input-requirement-row" data-requirement="'+item.id+'">'
          +'<span><b>'+escapeHtml(item.label)+'</b>'
          +'<small>'+escapeHtml(item.note)+'</small>'
          +(status.reason?'<small>'+escapeHtml(status.reason)+'</small>':'')+'</span>'
          +'<small>'+escapeHtml(status.label)+'</small></div>';
      }).join('')+'</div>';
  }).join('');
  return '<div class="parameter-note">必要输入清单只表达<b>需求等级</b>与<b>当前事实状态</b>：'
    +'必需项缺失会阻塞后续步骤；「可采用工程假设」的输入缺失时，系统会明确标注结果基于工程假设，'
    +'绝不静默补默认值。</div>'+rows;
}

/** B7X：归档 / compatibility 算法不得再成为新项目的 persisted selection。
 *  它们仍然注册（旧项目要能读、能由 compatibility adapter 解析），但不在生产算法设置里
 *  提供新选择；只有项目**当前已保存**的那个归档选择会作为现状选项保留，绝不静默改写。 */
const FROZEN_COMPATIBILITY_ALGORITHM_IDS=new Set([
  'route_planner_v1','risk_aware_route_planner_v2','layered_route_planner_v1',
  'coverage_planner_v1','cns_gap_analysis_v1','cns_gap_analysis_v2',
  'reuse_first_site_planner_v1',
]);

function algorithmSettings(flow){
  const labels={risk_model:'风险模型',route_planner:'航路规划',coverage_planner:'CNS覆盖规划',cns_gap_analyzer:'CNS缺口分析',coverage_model:'3D几何覆盖模型',service_model:'CNS静态服务能力模型',timeline_model:'运行服务时间线模型',protection_model:'战术保护包络模型',requirement_model:'CNS需求模型'};
  const selection=flow.algorithm_selection||{},catalog=flow.algorithm_catalog||[];
  const rows=Object.entries(labels).map(([type,label])=>{
    const current=selection[type]||{},items=catalog.filter(item=>item.algorithm_type===type&&!frozenCompatibilityOnly(item,current));
    const options=items.map(item=>'<option value="'+escapeHtml(algorithmSelectionKey(item))+'" '+(item.algorithm_id===current.algorithm_id&&item.version===current.version?'selected':'')+'>'+escapeHtml(item.name)+' · '+escapeHtml(item.algorithm_id)+'@'+escapeHtml(item.version)+'</option>').join('');
    const manifest=items.find(item=>item.algorithm_id===current.algorithm_id&&item.version===current.version);
    const info=algorithmManifestDetails(manifest);
    const detail=manifest?'<details class="algorithm-detail"><summary>详情：'+escapeHtml(manifest.name)+' · '+escapeHtml(info.maturity)+'</summary><p>'+escapeHtml(manifest.description)+'</p><small>Provider：'+escapeHtml(info.provider)+'<br>Inputs：'+escapeHtml(info.inputs)+'<br>Outputs：'+escapeHtml(info.outputs)+'<br>Parameters：'+escapeHtml(JSON.stringify(manifest.parameter_schema||{}))+'<br>Assumptions：'+escapeHtml(info.assumptions)+'<br>Limitations：'+escapeHtml(info.limitations)+'<br>References：'+escapeHtml(info.references)+'</small></details>':'<p class="inline-error">当前精确算法未注册</p>';
    return '<div class="algorithm-setting"><label>'+label+'</label><select class="panel-input" data-algorithm-select="'+type+'">'+options+'</select>'+detail+'</div>';
  }).join('');
  return '<div class="section-label">算法设置</div><div class="parameter-note">归档 / compatibility 实现不再作为新项目可选算法（正式实现见「航路规划」与「3D几何覆盖模型」）；旧项目已保存的归档选择保持原样，只在高级区读取或试算。</div><div class="algorithm-settings">'+rows+'</div>';
}

/** 归档/compatibility 算法：不是项目当前已保存的那个就必须从下拉中排除。 */
function frozenCompatibilityOnly(item,current){
  if(!FROZEN_COMPATIBILITY_ALGORITHM_IDS.has(item.algorithm_id))return false;
  return !(item.algorithm_id===current.algorithm_id&&item.version===current.version);
}

export function render({state,flow}){
  const storageDir=state?.project_storage?.directory||'';
  const cnsSources=[
    ['航空器能力配置',flow.aircraft_profiles],['设备资料库',flow.device_catalog],
    ['既有 CNS 设施',flow.existing_cns_facilities],['候选站址',flow.candidate_sites],
  ];
  const cnsStatus=cnsSources.map(([label,item])=>'<div class="list-row"><span>'+escapeHtml(label)+'</span><small>'+statusBadge(item?.status||'not_calculated')+' '+(item?.count||0)+' 项</small></div>').join('');
  const health=state?.data_health||{};
  const healthLabel={ready:'数据源正常',warning:'数据源有告警',error:'数据源错误',checking:'正在检查数据源'}[health.status]||health.status;
  const healthLine=wbLine(healthLabel||'数据源状态未知',health.status==='ready'?'ok':'warn',health.label||'');
  const project=flow.project||{};
  const workspace=flow.workspace;

  const objective='<div class="flow-summary"><b>本步目标</b>：准备项目容器与数据来源，'
    +'核对必要输入清单，确认哪些输入是必需的、哪些可以采用工程假设。'
    +'本步<b>不计算任何规划结果</b>，也不会因为打开项目就自动触发计算。</div>';

  const operate=wbPanel('operate',
    wbBlock('项目与数据来源',
      objective
      +'<label>项目名称</label><input class="panel-input" id="projectName" value="'+escapeHtml(project.name||'')+'">'
      +'<label>项目数据存储位置</label><div class="panel-file-input"><input class="panel-input" id="projectPath" value="'+escapeHtml(storageDir)+'" placeholder="请选择项目文件夹"><button class="secondary" id="browseProject">选择…</button></div>'
      +'<div class="parameter-note">数据来源（人口 / 地形 / 建筑 / 铁塔 / 空域要地 / 设备 / 起降点）'
      +'统一在顶部「数据源」对话框中配置与校验；本页只显示它们的健康状态。</div>'
      +'<button class="secondary full" id="openSettingsFromStep1">打开数据源设置</button>')
      +wbBlock('工作区',
        workspace
          ?'<div class="metric-grid"><b>'+(workspace.area_km2).toLocaleString()+' km²<small>工作区面积</small></b>'
            +'<b>'+escapeHtml(statusText((flow.grid||{}).status||'not_calculated'))+'<small>标准规划网格</small></b></div>'
            +'<div class="parameter-note">工作区在第 02 步框选与保存；本步不修改工作区。</div>'
          :'<div class="wb-empty">尚未保存工作区。请进入第 02 步「环境与风险」框选分析范围。</div>')
      +wbBlock('地名定位',
        '<label>地名搜索定位</label><input class="panel-input" id="placeSearch" type="search" placeholder="舟山市、朱家尖、普陀山"><button class="secondary full" id="searchPlace">搜索定位</button><div id="placeResults" class="place-results" hidden></div>')
      +wbBlock('项目动作',
        '<div class="button-row"><button class="secondary" id="openProject">打开项目</button></div>')
      +primaryAction('<button class="primary" id="saveProject">保存项目</button>',
        {note:'保存会把项目写入上面选择的文件夹；之后的自动保存也写入该项目。'})
  );

  const result=wbPanel('result',
    wbBlock('数据来源健康状态',healthLine,statusBadge(health.status||'unknown'))
      +wbBlock('必要输入清单',inputRequirementPanel({state,flow}))
      +wbBlock('规划输入就绪',
        '<div class="scroll-list">'+(cnsStatus||wbEmpty('尚未载入规划输入数据'))+'</div>')
      +wbBlock('阻塞项与工程假设',blockerList(projectBlockerItems({state,flow}),
        '当前没有阻塞项；必要输入清单中标注「可采用工程假设」的项目若缺失，结果会带工程假设提示。'))
  );

  const advanced=wbPanel('advanced',
    wbBlock('算法选择',
      '<div class="parameter-note">每个算法类型使用注册表中精确的 id@version；未注册的精确版本不会回退到其他版本。</div>'
      +algorithmSettings(flow))
      +wbBlock('高级 / 审计信息',
        advancedAuditNote('数据来源审计（source hash / schema / algorithm manifest）在数据源中心与项目结果索引中查看；'
          +'本页只显示健康状态与需求等级。')
        +'<div class="flow-summary">'+wbEngine([
          'project revision '+(project.revision??'—'),
          'workflow revision '+(flow.revision??'—'),
          'grid level '+String((flow.grid||{}).level??'—'),
          'algorithm catalog '+(flow.algorithm_catalog||[]).length+' 项'
        ])+'</div>')
  );

  return shell('01','数据准备','准备项目与数据来源，核对必要输入清单，确认必需项与可采用工程假设的输入。',
    operate+result+advanced
    +nextStepBar({
      enabled:Boolean(flow.steps&&flow.steps['1']),
      label:'下一步：环境与风险',
      reason:flow.steps&&flow.steps['1']?'':'请先选择项目存储位置并保存项目',
      note:'第 02 步负责框选工作区、建立环境模型与航路风险场。'
    }));
}

/** Step1 的阻塞项 / 工程假设（只读 flow / state，不推断）。 */
function projectBlockerItems({state,flow}){
  const items=[];
  const storage=(state&&state.project_storage)||{};
  if(!storage.directory){
    items.push({kind:'blocker',text:'尚未选择项目数据存储位置',
      detail:'没有存储位置时项目无法保存，也无法在重启后恢复。'});
  }
  const paths=(state&&state.paths)||{};
  const missing=INPUT_REQUIREMENTS.filter(item=>item.level==='required'&&!inputRequirementStatus(item,{state,flow}).configured);
  if(missing.length){
    items.push({kind:'blocker',text:'必需输入尚未配置：'+missing.map(item=>item.label).join('、'),
      detail:'必需项缺失会阻塞依赖它的规划步骤；请先在数据源中心登记对应文件。'});
  }
  const assumable=INPUT_REQUIREMENTS.filter(item=>item.level==='assumable'&&!inputRequirementStatus(item,{state,flow}).configured);
  if(assumable.length){
    items.push({kind:'assumption',text:'可采用工程假设的输入尚未配置：'+assumable.map(item=>item.label).join('、'),
      detail:'缺失时相关判定会保持证据不足；系统不会替你选择工程假设值。'});
  }
  const health=(state&&state.data_health)||{};
  if(health.status&&health.status!=='ready'&&health.status!=='checking'){
    items.push({kind:'blocker',text:'数据来源健康状态：'+sourceStateText(health.status),
      detail:String(health.label||'请在数据源中心查看明细')});
  }
  return items;
}

export function bind(c){
  c.$('browseProject').onclick=()=>c.openBrowser('project',c.$('projectPath').value);
  c.$('saveProject').onclick=()=>c.saveProject(c.$('projectPath').value.trim(),c.$('projectName').value.trim());
  c.$('openProject').onclick=()=>c.openProject(c.$('projectPath').value.trim());
  // 数据源对话框挂在应用外壳上（不在工作台面板内），因此这里直接按 id 查找，
  // 不走 `c.$()`（它是"工作台内必须命中"的查询契约）。
  const settings=typeof document!=='undefined'&&document.getElementById?document.getElementById('settings'):null;
  if(settings&&c.$('openSettingsFromStep1'))c.$('openSettingsFromStep1').onclick=()=>settings.showModal();
  c.$('searchPlace').onclick=c.searchPlace;c.$('placeSearch').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();c.searchPlace();}};
  document.querySelectorAll('[data-algorithm-select]').forEach(select=>select.onchange=async()=>{
    const [algorithm_type,algorithm_id,version]=select.value.split('|');
    const parameters=c.flow().algorithm_selection?.[algorithm_type]?.parameters||{};
    try{await c.resourceAction('/api/algorithms/select',{algorithm_type,algorithm_id,version,parameters});}
    catch(error){c.panelError('算法切换失败：'+error.message);}
  });
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(2);
}
