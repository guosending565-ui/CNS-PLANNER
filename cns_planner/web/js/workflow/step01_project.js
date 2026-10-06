import {escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock,wbEmpty,wbLine,wbEngine,wbCard,
  inputRequirementText,inputRequirementBadge,sourceStateText,workflowStatusText,emptyReasonText,
  advancedAuditNote,blockerList,primaryAction,nextStepBar} from './common.js';
import {workspaceGridReadiness} from '../state/readiness.js';

/**
 * =========================================================================
 * 项目打开状态（BUG-PROJECT-OPEN-UX-001 的唯一 UI 状态）
 * =========================================================================
 *
 * 修复前的真实缺陷：Step01 只有「选择…」+「打开项目」两个按钮，但
 *   * 「选择…」返回后**没有任何反馈**——用户不知道只是选中了目录、还是项目
 *     已经在后台被切换；
 *   * 输入框的值来自 `state.project_storage.directory`（**服务器当前项目**），
 *     面板一旦重渲染就会被服务器值覆盖，用户刚选的路径凭空消失；
 *   * 「打开项目」既没有进行中状态，也没有成功/失败结论，失败只落在 panelError。
 *
 * 现在把语义彻底分开，并且只允许三种事实来源：
 *   1. `projectOpen.draft`  —— 用户刚在目录选择器里选中的路径（**只是选中，不是打开**）；
 *   2. `projectOpen.identity` —— 上一次成功打开的服务器 active project 身份；
 *   3. `projectOpen.message` —— 服务器返回的明确结论（中文业务原因，失败不吞）。
 *
 * 本模块**不猜**项目是否已打开：`step` 由 main.js 在真实服务器事实之上给出。
 */
export const PROJECT_OPEN_STATE_TEXT={
  empty:'尚未选择项目目录。',
  selected:'已选择项目目录，尚未打开项目。',
  opening:'正在打开项目…',
  //: 新建项目在途时的文案：不能把"新建"说成"打开"（事实口径必须如实）。
  creating:'正在新建项目…',
  opened:'项目已打开',
  //: 服务器 active project 身份优先于 draft 的"已选择"状态时使用（F-03 §状态一致性）。
  active:'当前项目已激活',
  //: 新建空白项目成功后的唯一结论文案。
  created:'新建项目完成，当前项目已激活',
  failed:'打开项目失败',
};

/** 打开过程的可见阶段文案（与 main.js 的 PROJECT_OPEN_STAGES 一一对应）。 */
export const PROJECT_OPEN_STAGE_TEXT={
  opening:'正在切换服务器 active project…',
  applying:'正在安装项目状态与工作流快照…',
  refreshing:'正在校准会话令牌与项目存储位置…',
  map:'正在初始化地图视图与工作区图层…',
  opened:'项目已打开',
};

/**
 * A2：**同一项目**再次打开时的阶段文案。
 *
 * 此时不允许再出现"正在切换服务器 active project"——服务器上一次都没有切换。
 * 这里只如实说明"确认当前项目 + 刷新状态"。
 */
export const PROJECT_OPEN_SAME_STAGE_TEXT={
  opening:'正在确认当前项目…',
  refreshing:'正在刷新当前项目状态…',
  opened:'当前项目已激活',
};

/**
 * 对比两个输入框里的路径是否指向同一个目录。
 *
 * 只做**纯文本**归一化（分隔符 / 尾部斜杠 / Windows 大小写），不解析、不猜、
 * 不访问文件系统（浏览器里也没有这个能力）。
 */
export function sameProjectPath(a,b){
  const normalize=value=>String(value||'').trim().replace(/[\\/]+$/,'').replace(/\//g,'\\').toLowerCase();
  const left=normalize(a),right=normalize(b);
  return Boolean(left)&&left===right;
}

/** 路径最后一段（仅用于显示名，不参与任何判定）。 */
function projectBasename(value){
  const parts=String(value||'').trim().replace(/[\\/]+$/,'').split(/[\\/]/);
  return parts[parts.length-1]||'';
}

function formatNumber(value){
  const number=Number(value);
  return Number.isFinite(number)?number.toLocaleString():'—';
}

/** 标准规划网格的一行事实（passed / blocked / 未生成三种如实转印）。 */
function gridFactText(facts){
  if(!facts)return '未生成';
  if(facts.grid)return 'L'+String(facts.gridLevel??8)+' · '+formatNumber(facts.gridCells)+' 格';
  if(facts.gridBlocked)return '已阻断（请缩小工作区或提高上限后重新保存工作区）';
  return '未生成';
}

/** 工作区一行事实。 */
function workspaceFactText(facts){
  if(!facts)return '未配置';
  if(!facts.workspace)return '未配置';
  const area=Number.isFinite(Number(facts.areaKm2))?formatNumber(facts.areaKm2)+' km²':'范围已保存（面积未记录）';
  return '已加载 · '+area;
}

/**
 * 「项目数据存储位置 + 选择… + 打开项目 + 状态」的完整只读投影。
 *
 * @param {{flow?:object, getStep?:()=>object|null, draft?:string}} input
 */
export function projectOpenPanel({getStep,draft=''}={}){
  const step=(typeof getStep==='function'?getStep():null)||{};
  const busy=_projectOpenInFlight;                       // 打开中：由模块级在途标志裁决
  const savedIdentity=String(step.identity||'');         // 服务器 active project（已打开）
  const savedDirectory=String(step.directory||'');
  const currentInput=String(draft||'');
  const isCurrent=sameProjectPath(currentInput,savedDirectory);
  const statusKey=openStatusKey(step);
  const statusText=PROJECT_OPEN_STATE_TEXT[statusKey]||'';
  const buttonLabel=busy?'正在打开…':(isCurrent&&savedIdentity?'确认当前项目':'打开项目');
  const stageText=(projectOpen.sameProject?PROJECT_OPEN_SAME_STAGE_TEXT:PROJECT_OPEN_STAGE_TEXT)[projectOpen.stage]
    ||(projectOpen.sameProject?PROJECT_OPEN_SAME_STAGE_TEXT.opening:PROJECT_OPEN_STAGE_TEXT.opening);
  const facts=statusFacts(statusKey,step);
  const rows=[
    '<div class="project-open-status" id="projectOpenStatus" data-state="'+statusKey+'" aria-live="polite">'
      +'<b>'+escapeHtml(statusText)+'</b>'
      +(busy?'<small>'+escapeHtml(stageText)+'</small>':'')
      +(statusKey==='failed'&&projectOpen.message?'<small class="project-open-error">'+escapeHtml(projectOpen.message)+'</small>':'')
      +((statusKey==='opened'&&facts)
        ?'<small>项目名称：'+escapeHtml(facts.name||'未命名项目')+'</small>'
          +'<small>项目目录：'+escapeHtml(projectOpen.displayDirectory||savedDirectory||'（服务器自动恢复项目）')+'</small>'
          +'<small>工作区：'+escapeHtml(workspaceFactText(facts))+'</small>'
          +'<small>标准规划网格：'+escapeHtml(gridFactText(facts))+'</small>'
        :'')
    +'</div>'
  ];
  if(savedIdentity&&savedDirectory&&!isCurrent&&!busy){
    rows.push('<div class="parameter-note">服务器当前已打开的项目：'
      +escapeHtml(projectBasename(savedDirectory)||savedDirectory)
      +'；点击「打开项目」会切换到上面输入的目录。</div>');
  }
  if(isCurrent&&savedIdentity&&!busy){
    rows.push('<div class="parameter-note">当前项目已激活；再次点击会做一次轻量状态刷新，不再重新载入项目。</div>');
  }
  return '<label>项目数据存储位置</label>'
    +'<div class="panel-file-input"><input class="panel-input" id="projectPath" value="'+escapeHtml(currentInput)+'" placeholder="请选择项目文件夹"><button class="secondary" id="browseProject">选择…</button>'
    +(busy?'':'<button class="secondary" id="openProject" '+(currentInput?'':'disabled')+'>'+escapeHtml(buttonLabel)+'</button>')
    +'</div>'
    +rows.join('')
    +newProjectBlock(busy);
}

/**
 * F-03：**新建空白项目**动作区。
 *
 * 目录沿用上面的「项目数据存储位置」（「选择…」已经能把它选出来），只额外要一个项目名称，
 * 因此普通用户不需要先手工建目录、也不需要先「另存为」才能从零开始。
 *
 * 文案必须如实说明"不继承"：新建出来的是**完全空白**的项目，不是当前项目的副本。
 */
function newProjectBlock(busy){
  return '<label>新建项目</label>'
    +'<div class="parameter-note">用上面的「项目数据存储位置」作为新项目目录（必须是空目录，已存在的项目不会被覆盖）。'
      +'点击「新建项目」会创建一个<b>完全空白</b>的新项目并立即激活：不复制当前项目的 workspace / 航路 / 候选 / 风险画像 / 报告，'
      +'数据源按公共默认配置初始化。</div>'
    +'<div class="panel-file-input"><input class="panel-input" id="newProjectName" placeholder="项目名称（1–120 字符）" value="'+escapeHtml(_newProjectName)+'">'
    +(busy?'':'<button class="secondary" id="createProject">新建项目</button>')
    +'</div>';
}

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
  // 服务器当前项目目录（权威事实，只在渲染时读取）：draft 为空时它就是输入框的值。
  // 自动恢复项目的 directory 为空 —— 此时输入框留空，绝不编造一个"看起来像项目"的路径。
  _serverProjectDirectory=String(state?.project_storage?.directory||'');
  const cnsSources=[
    ['航空器能力配置',flow.aircraft_profiles],['设备资料库',flow.device_catalog],
    ['既有 CNS 设施',flow.existing_cns_facilities],['候选站址',flow.candidate_sites],
  ];
  const cnsStatus=cnsSources.map(([label,item])=>'<div class="list-row"><span>'+escapeHtml(label)+'</span><small>'+statusBadge(item?.status||'not_calculated')+' '+(item?.count||0)+' 项</small></div>').join('');
  const health=state?.data_health||{};
  const healthLabel={ready:'数据源正常',warning:'数据源有告警',error:'数据源错误',checking:'正在检查数据源'}[health.status]||health.status;
  const healthLine=wbLine(healthLabel||'数据源状态未知',health.status==='ready'?'ok':'warn',health.label||'');
  const project=flow.project||{};
  // BUG-PROJECT-OPEN-UX-002：workspace 存在但 area_km2 缺失时，`(undefined).toLocaleString()`
  // 会让整个 Step01 以技术错误中断。这里只做**空值保护**，不补 0、不推断面积。
  const workspaceArea=Number(flow.workspace?.area_km2);
  const workspace=flow.workspace
    ?{...flow.workspace,area_km2:Number.isFinite(workspaceArea)?workspaceArea:null}
    :null;

  const objective='<div class="flow-summary"><b>本步目标</b>：准备项目容器与数据来源，'
    +'核对必要输入清单，确认哪些输入是必需的、哪些可以采用工程假设。'
    +'本步<b>不计算任何规划结果</b>，也不会因为打开项目就自动触发计算。</div>';

  const operate=wbPanel('operate',
    wbBlock('项目与数据来源',
      objective
      +'<label>项目名称</label><input class="panel-input" id="projectName" value="'+escapeHtml(project.name||'')+'">'
      +projectOpenPanel({getStep:()=>projectOpen,draft:draftDirectory()})
      +'<div class="parameter-note">数据来源（人口 / 地形 / 建筑 / 铁塔 / 空域要地 / 设备 / 起降点）'
      +'统一在顶部「数据源」对话框中配置与校验；本页只显示它们的健康状态。</div>'
      +'<button class="secondary full" id="openSettingsFromStep1">打开数据源设置</button>')
      +wbBlock('工作区',
        workspace
          ?'<div class="metric-grid"><b>'+(workspace.area_km2===null?'—':workspace.area_km2.toLocaleString())+' km²<small>工作区面积</small></b>'
            +'<b>'+escapeHtml(statusText((flow.grid||{}).status||'not_calculated'))+'<small>标准规划网格</small></b></div>'
            +'<div class="parameter-note">工作区在第 02 步框选与保存；本步不修改工作区。</div>'
          :'<div class="wb-empty">尚未保存工作区。请进入第 02 步「环境与风险」框选分析范围。</div>')
      +wbBlock('地名定位',
        '<label>地名搜索定位</label><input class="panel-input" id="placeSearch" type="search" placeholder="舟山市、朱家尖、普陀山"><button class="secondary full" id="searchPlace">搜索定位</button><div id="placeResults" class="place-results" hidden></div>')
      +wbBlock('项目动作',
        // BUG-PROJECT-OPEN-UX-001：这里曾经再放一个「打开项目」按钮，与"项目数据存储位置"
        // 下面的按钮重名同 id、语义重复。打开项目的**唯一**按钮与状态提示现在都在
        // 「项目数据存储位置」块里（见 projectOpenPanel），此处只保留保存动作。
        '<div class="parameter-note">打开项目在「项目数据存储位置」处显式提交；本步不会在后台偷偷切换项目。</div>')
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

/**
 * Step01 的项目打开 UI 状态（模块级，因为它在多次面板重渲染之间必须存活）。
 *
 * `draft`      用户刚选中的目录（仅选中，**不代表**已打开）；
 * `busy`       正在执行 POST /api/project/open 及安装链；
 * `ok/message` 服务器给出的明确结论；`facts` 打开完成后的真实项目事实。
 */
let _draftProjectDirectory='';
/** 服务器 active project 目录（每次 render 由 render() 刷新；bind 绝不覆盖 draft）。 */
let _serverProjectDirectory='';
/** 打开项目是否正在进行。必须是模块级：面板重渲染不能让"正在打开…"凭空消失。 */
let _projectOpenInFlight=false;
let projectOpen={busy:false,ok:false,message:'',stage:'',identity:'',displayDirectory:'',facts:null,created:false,creating:false};
/** 新建项目表单里的项目名称（模块级：面板重渲染不得清空用户已输入的名称）。 */
let _newProjectName='';

/** 目录选择器的选择结果（壳层调用）：只写 draft，绝不打开项目。 */
export function setProjectDirectoryDraft(directory){
  const value=String(directory||'').trim();
  if(value)_draftProjectDirectory=value;
  return _draftProjectDirectory;
}

/**
 * 显示给用户的 draft 目录。
 *
 * 稳定优先级（**不再依赖 bind 时机**）：
 *   1. 用户刚选中的目录 / 刚手工编辑的路径（`_draftProjectDirectory`）；
 *   2. 服务器当前 active project 目录 —— 仅在**渲染时**服务器已明确有项目时取值。
 *
 * 第 2 条只在渲染那一刻读权威事实，因此不会出现"面板重渲染把用户刚选的路径覆盖回
 * 旧项目目录"这一类问题（BUG-PROJECT-OPEN-UX-001 §C 明确禁止）。
 */
function currentProjectDirectory(){
  return _draftProjectDirectory||_serverProjectDirectory;
}

/** 当前 draft：优先用户输入框的实时值，其次上一次选择结果。 */
function draftDirectory(){return currentProjectDirectory();}

/**
 * 服务器已确认的 active project 目录。
 *
 * 两个来源都**只**来自服务器事实：本次会话内最近一次成功结论（``displayDirectory``），
 * 或启动/渲染时 main.js 提供的 active project 身份（``step.directory``）。绝不猜路径。
 */
function serverActiveDirectory(step){
  // ``_serverProjectDirectory`` 是每次 render 从 ``state.project_storage.directory`` 取到的
  // 权威值（automatic 项目为空串），因此它最强；其次是本次会话的结论与渲染时传入的 step。
  return String(_serverProjectDirectory||projectOpen.displayDirectory||(step&&step.directory)||'');
}

/**
 * 当前项目打开状态的**唯一**判读（面板与增量刷新共用，避免两处各写一套 if/else）。
 *
 * 事实优先级：
 *   busy（模块级在途标志） > 服务器返回的结论（message/ok） > **服务器 active project
 *   身份** > draft 是否存在。
 *
 * F-03 §状态一致性：项目已经激活时，不允许再显示"已选择项目目录，尚未打开项目"——
 * 服务器 active project 身份优先于 draft 的"仅选中"状态（Round32-A M-01 的成因）。
 */
function openStatusKey(step){
  if(_projectOpenInFlight)return projectOpen.creating?'creating':'opening';
  if(projectOpen.message)return projectOpen.ok?'opened':'failed';
  if(projectOpen.created)return 'created';
  if(serverActiveDirectory(step))return 'active';
  return draftDirectory()?'selected':'empty';
}

/** 会展示"项目事实"（名称/目录/工作区/网格）的状态：已打开、已激活、新建完成。 */
const FACTS_STATUS_KEYS=['opened','active','created'];

/** 当前状态可用的项目事实：本次结论优先，其次服务器 active project 身份携带的事实。 */
function statusFacts(statusKey,step){
  if(!FACTS_STATUS_KEYS.includes(statusKey))return null;
  return projectOpen.facts||(step&&step.facts)||null;
}

/** 只刷新状态提示区，不重建整个面板（避免输入焦点与滚动位置丢失）。 */
function renderOpenStatus(node){
  if(!node)return;
  const saved=node.__projectOpenStep||{};
  const busy=_projectOpenInFlight||saved.busy===true;
  const statusKey=openStatusKey(saved);
  node.dataset.state=statusKey;
  const lines=[];
  lines.push('<b>'+escapeHtml(PROJECT_OPEN_STATE_TEXT[statusKey]||'')+'</b>');
  if(busy)lines.push('<small>'+escapeHtml(PROJECT_OPEN_STAGE_TEXT[projectOpen.stage]||PROJECT_OPEN_STAGE_TEXT.opening)+'</small>');
  if(statusKey==='failed'&&projectOpen.message)lines.push('<small class="project-open-error">'+escapeHtml(projectOpen.message)+'</small>');
  const facts=statusFacts(statusKey,saved);
  if(facts){
    lines.push('<small>项目名称：'+escapeHtml(facts.name||'未命名项目')+'</small>');
    lines.push('<small>项目目录：'+escapeHtml(projectOpen.displayDirectory||saved.directory||'（服务器自动恢复项目）')+'</small>');
    lines.push('<small>工作区：'+escapeHtml(workspaceFactText(facts))+'</small>');
    lines.push('<small>标准规划网格：'+escapeHtml(gridFactText(facts))+'</small>');
  }
  node.innerHTML=lines.join('');
}

/**
 * Step01 绑定（BUG-PROJECT-OPEN-UX-001）。
 *
 * 语义边界（必须彻底分开）：
 *   「选择…」  → 只打开目录选择器，把结果写进输入框（draft）+ 提示"已选择项目目录，
 *                尚未打开项目"。**绝不**发任何项目切换请求。
 *   「打开项目」→ 才调用唯一的 `openProject()`，并显示 正在打开… → 项目已打开 / 打开失败。
 */
export function bind(c){
  const pathInput=c.$('projectPath');
  const openButton=c.$('openProject');
  const statusNode=c.$('projectOpenStatus');
  projectOpen=Object.assign({busy:false,ok:false,message:'',stage:'',identity:'',displayDirectory:'',facts:null,sameProject:false},projectOpen||{});
  // 「正在打开…」必须跨重渲染存活：main.js 会在每个阶段重绘面板（用户要求看得见进度）。
  projectOpen.busy=_projectOpenInFlight;
  // 输入框显示 draft（用户刚选的 / 刚输入的）优先；否则显示服务器当前项目目录。
  // 这里**只**赋值给输入框，不改 draft —— 保证任何一次重渲染都不会覆盖用户的选择。
  if(pathInput)pathInput.value=currentProjectDirectory();
  if(statusNode)statusNode.__projectOpenStep=c.projectOpenStep?.()||{};
  renderOpenStatus(statusNode);

  const syncButtonLabel=()=>{
    if(!openButton)return;
    const step=(statusNode&&statusNode.__projectOpenStep)||{};
    const isCurrent=Boolean(step.identity)&&sameProjectPath(pathInput?pathInput.value:'',step.directory);
    openButton.disabled=projectOpen.busy;
    openButton.textContent=projectOpen.busy?'正在打开…':(isCurrent?'确认当前项目':'打开项目');
  };
  syncButtonLabel();

  if(c.$('browseProject'))c.$('browseProject').onclick=()=>c.openBrowser('project',pathInput?pathInput.value:'');
  // 「选择…」的回写由目录对话框负责；这里只跟踪用户对输入框的显式编辑，
  // 保证 draft 始终等于输入框里的真实值（粘贴路径也算数）。
  if(pathInput){
    const onDraft=()=>{_draftProjectDirectory=pathInput.value;syncButtonLabel();};
    pathInput.oninput=onDraft;pathInput.onchange=onDraft;
    pathInput.onkeydown=event=>{
      if(event.key!=='Enter')return;
      event.preventDefault();
      if(openButton&&!openButton.disabled)openButton.click();
    };
  }
  if(openButton){
    openButton.onclick=async()=>{
      if(_projectOpenInFlight)return;
      const directory=String(pathInput?pathInput.value:'').trim();
      _draftProjectDirectory=directory;
      _projectOpenInFlight=true;
      projectOpen.ok=false;projectOpen.message='';projectOpen.stage='opening';
      // 重新走「打开项目」必然清掉上一次"新建项目完成"的结论，避免陈旧结论文案。
      projectOpen.created=false;
      // A2：先按服务器当前 active 项目判据给阶段文案定性。真正裁决仍在 main.js /
      // 后端幂等保护（这里只决定"正在确认当前项目"还是"正在切换服务器 active project"）。
      const openedStep=(statusNode&&statusNode.__projectOpenStep)||{};
      projectOpen.sameProject=Boolean(openedStep.identity)
        &&sameProjectPath(directory,openedStep.directory);
      syncButtonLabel();
      renderOpenStatus(statusNode);
      let result=null;
      try{
        // 唯一的项目安装调用链（实现在 main.js）：只转发阶段，不复制任何安装逻辑。
        // 每个阶段 main.js 都会 renderWorkflow() 重绘面板，因此这里不再直接改 DOM。
        result=await c.openProject(directory,{onStage:stage=>{
          projectOpen.stage=stage;
          renderOpenStatus(statusNode);
        }});
      }catch(error){
        // 唯一调用链内部已把失败转成中文结论；这里只兜底"连调用都没成功"的极端情况。
        result={ok:false,error:error instanceof Error?error:new Error(String(error)),facts:null};
      }finally{
        _projectOpenInFlight=false;
      }
      projectOpen.ok=Boolean(result?.ok);
      projectOpen.message=result?.ok?'':String(result?.error?.message||'打开项目失败');
      projectOpen.identity=String(result?.identity||'');
      projectOpen.displayDirectory=result?.ok?directory:'';
      projectOpen.facts=result?.facts||null;
      if(statusNode)statusNode.__projectOpenStep={busy:false,identity:projectOpen.identity,
        directory:projectOpen.displayDirectory,facts:projectOpen.facts};
      // 成功 / 失败都必须落到可见结论（服务器事实，不猜）。
      renderOpenStatus(statusNode);
      syncButtonLabel();
    };
  }
  c.$('saveProject').onclick=()=>c.saveProject(pathInput?pathInput.value.trim():'',c.$('projectName').value.trim());
  // F-03：新建空白项目。目录沿用输入框（与「选择…」共用同一个 draft 来源），
  // 这里只负责表单校验与结论落地，真正的创建 + 安装链在 main.js 的 createProject()。
  const newProjectNameInput=c.$('newProjectName');
  const createProjectButton=c.$('createProject');
  if(newProjectNameInput){
    const onName=()=>{_newProjectName=newProjectNameInput.value;};
    newProjectNameInput.oninput=onName;newProjectNameInput.onchange=onName;
  }
  if(createProjectButton){
    createProjectButton.onclick=async()=>{
      const directory=String(pathInput?pathInput.value:'').trim();
      const name=String(newProjectNameInput?newProjectNameInput.value:'').trim();
      if(!directory){c.panelError('请先选择新项目的项目目录','error');return;}
      if(!name){c.panelError('请填写项目名称（1–120 字符）','error');return;}
      createProjectButton.disabled=true;
      projectOpen.ok=false;projectOpen.message='';projectOpen.created=false;
      projectOpen.creating=true;projectOpen.stage='opening';
      _projectOpenInFlight=true;
      renderOpenStatus(statusNode);
      let result=null;
      try{
        result=await c.createProject(directory,name,{onStage:stage=>{
          projectOpen.stage=stage;
          renderOpenStatus(statusNode);
        }});
      }catch(error){
        result={ok:false,error:error instanceof Error?error:new Error(String(error)),facts:null};
      }finally{
        _projectOpenInFlight=false;
        projectOpen.creating=false;
        projectOpen.busy=false;
      }
      projectOpen.ok=Boolean(result?.ok);
      projectOpen.created=Boolean(result?.ok);
      projectOpen.message=result?.ok?'':String(result?.error?.message||'新建项目失败');
      projectOpen.identity=String(result?.identity||'');
      projectOpen.displayDirectory=result?.ok?directory:'';
      projectOpen.facts=result?.facts||null;
      if(result?.ok)_newProjectName='';
      if(statusNode&&document.body.contains(statusNode)){
        statusNode.__projectOpenStep={busy:false,identity:projectOpen.identity,
          directory:projectOpen.displayDirectory,facts:projectOpen.facts};
        renderOpenStatus(statusNode);
      }
      const liveButton=document.getElementById('createProject');
      if(liveButton)liveButton.disabled=false;
    };
  }
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
