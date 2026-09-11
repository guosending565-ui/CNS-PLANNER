import {escapeHtml,shell,statusBadge} from './common.js';
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
    '<div class="flow-summary">'+statusBadge(state.data_health.status)+' '+state.data_health.label+'<br>'+state.layers.length+' 个本地图层 · 本地数据可独立工作</div><div class="scroll-list">'+cnsStatus+'</div><button class="primary full" id="nextStep">下一步：工作区</button>';
  return shell('01','项目与数据','设置项目名称和数据存储位置，检查数据后进入工作区。',body);
}
export function bind(c){
  c.$('browseProject').onclick=()=>c.openBrowser('project',c.$('projectPath').value);
  c.$('saveProject').onclick=()=>c.saveProject(c.$('projectPath').value.trim(),c.$('projectName').value.trim());
  c.$('openProject').onclick=()=>c.openProject(c.$('projectPath').value.trim());
  c.$('searchPlace').onclick=c.searchPlace;c.$('placeSearch').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();c.searchPlace();}};
  c.$('nextStep').onclick=()=>c.setStep(2);
}
