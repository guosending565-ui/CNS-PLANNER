import {escapeHtml,shell,statusBadge} from './common.js';

export function algorithmSelectionKey(item){return [item?.algorithm_type,item?.algorithm_id,item?.version].join('|');}
export function algorithmManifestDetails(item){return item?{
  identity:item.algorithm_id+'@'+item.version,provider:item.provider,maturity:item.maturity,
  inputs:(item.inputs||[]).join(', '),outputs:(item.outputs||[]).join(', '),
  assumptions:(item.assumptions||[]).join('；')||'无',limitations:(item.limitations||[]).join('；')||'无',
  references:(item.references||[]).join('；')||'未登记'
}:null;}

function algorithmSettings(flow){
  const labels={risk_model:'风险模型',route_planner:'航路规划',coverage_planner:'CNS覆盖规划',cns_gap_analyzer:'CNS缺口分析',coverage_model:'3D几何覆盖模型',service_model:'CNS静态服务能力模型'};
  const selection=flow.algorithm_selection||{},catalog=flow.algorithm_catalog||[];
  const rows=Object.entries(labels).map(([type,label])=>{
    const current=selection[type]||{},items=catalog.filter(item=>item.algorithm_type===type);
    const options=items.map(item=>'<option value="'+escapeHtml(algorithmSelectionKey(item))+'" '+(item.algorithm_id===current.algorithm_id&&item.version===current.version?'selected':'')+'>'+escapeHtml(item.name)+' · '+escapeHtml(item.algorithm_id)+'@'+escapeHtml(item.version)+'</option>').join('');
    const manifest=items.find(item=>item.algorithm_id===current.algorithm_id&&item.version===current.version);
    const info=algorithmManifestDetails(manifest);
    const detail=manifest?'<details class="algorithm-detail"><summary>详情：'+escapeHtml(manifest.name)+' · '+escapeHtml(info.maturity)+'</summary><p>'+escapeHtml(manifest.description)+'</p><small>Provider：'+escapeHtml(info.provider)+'<br>Inputs：'+escapeHtml(info.inputs)+'<br>Outputs：'+escapeHtml(info.outputs)+'<br>Parameters：'+escapeHtml(JSON.stringify(manifest.parameter_schema||{}))+'<br>Assumptions：'+escapeHtml(info.assumptions)+'<br>Limitations：'+escapeHtml(info.limitations)+'<br>References：'+escapeHtml(info.references)+'</small></details>':'<p class="inline-error">当前精确算法未注册</p>';
    return '<div class="algorithm-setting"><label>'+label+'</label><select class="panel-input" data-algorithm-select="'+type+'">'+options+'</select>'+detail+'</div>';
  }).join('');
  return '<div class="section-label">算法设置</div><div class="algorithm-settings">'+rows+'</div>';
}

export function render({state,flow}){
  const storageDir=state?.project_storage?.directory||'';
  const cnsSources=[
    ['Aircraft Profile',flow.aircraft_profiles],['Device Catalog',flow.device_catalog],
    ['Existing CNS',flow.existing_cns_facilities],['Candidate Site',flow.candidate_sites],
  ];
  const cnsStatus=cnsSources.map(([label,item])=>'<div class="list-row"><span>'+escapeHtml(label)+'</span><small>'+statusBadge(item?.status||'not_calculated')+' '+(item?.count||0)+' 项</small></div>').join('');
  const body='<label>项目名称</label><input class="panel-input" id="projectName" value="'+escapeHtml(flow.project.name)+'">'+
    '<label>项目数据存储位置</label><div class="panel-file-input"><input class="panel-input" id="projectPath" value="'+escapeHtml(storageDir)+'" placeholder="请选择项目文件夹"><button class="secondary" id="browseProject">选择…</button></div>'+
    '<div class="button-row project-buttons"><button class="secondary" id="openProject">打开项目</button><button class="primary" id="saveProject">保存项目</button></div>'+
    '<div class="project-path-note">保存后将在该目录生成 project_state.json 和 data_sources.json；之后的自动保存将写入该项目。</div>'+
    '<label>地名搜索定位</label><input class="panel-input" id="placeSearch" type="search" placeholder="舟山市、朱家尖、普陀山"><button class="secondary full" id="searchPlace">搜索定位</button><div id="placeResults" class="place-results" hidden></div>'+
    '<div class="flow-summary">'+statusBadge(state.data_health.status)+' '+state.data_health.label+'<br>'+state.layers.length+' 个本地图层 · 本地数据可独立工作</div><div class="scroll-list">'+cnsStatus+'</div>'+algorithmSettings(flow)+'<button class="primary full" id="nextStep">下一步：工作区</button>';
  return shell('01','项目与数据','设置项目名称和数据存储位置，检查数据后进入工作区。',body);
}
export function bind(c){
  c.$('browseProject').onclick=()=>c.openBrowser('project',c.$('projectPath').value);
  c.$('saveProject').onclick=()=>c.saveProject(c.$('projectPath').value.trim(),c.$('projectName').value.trim());
  c.$('openProject').onclick=()=>c.openProject(c.$('projectPath').value.trim());
  c.$('searchPlace').onclick=c.searchPlace;c.$('placeSearch').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();c.searchPlace();}};
  document.querySelectorAll('[data-algorithm-select]').forEach(select=>select.onchange=async()=>{
    const [algorithm_type,algorithm_id,version]=select.value.split('|');
    const parameters=c.flow().algorithm_selection?.[algorithm_type]?.parameters||{};
    try{await c.resourceAction('/api/algorithms/select',{algorithm_type,algorithm_id,version,parameters});}
    catch(error){c.panelError('算法切换失败：'+error.message);}
  });
  c.$('nextStep').onclick=()=>c.setStep(2);
}
