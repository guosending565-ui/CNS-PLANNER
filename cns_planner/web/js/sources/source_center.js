import {escapeHtml,sourceModeText,statusText} from '../workflow/common.js';

export function createSourceCenter({$,api,onlineTiles,onApplied,actionButton}){
  let browseKind='basemap',browseParent='',selectedFile='';
  const healthLabel=status=>({ready:'正常',warning:'警告',error:'错误',checking:'检查中'}[status]||statusText(status));
  function render(data){
    const health=data.data_health||{status:'checking',label:'正在检查数据源',items:[]},connection=$('connection');
    connection.className='status status-'+health.status;connection.innerHTML='<i class="dot"></i><span>'+health.label+'</span>';connection.title='打开统一数据源设置查看检查明细';
    const summary=$('healthSummary');summary.className='health-summary health-'+health.status;summary.textContent=health.label+' · 文件级检查；工作区覆盖在“工作区”步骤检查';
    const list=$('sourceHealthList');list.replaceChildren();
    for(const item of health.items){
      const row=document.createElement('article');row.className='source-health-item';row.dataset.sourceId=item.id;
      const crs=typeof item.crs==='object'?[item.crs.horizontal,item.crs.vertical].filter(Boolean).join(' / '):(item.crs||'');
      const resolution=typeof item.resolution==='object'?(item.resolution.nominal||[item.resolution.angular_value,item.resolution.angular_unit].filter(value=>value!==undefined).join(' ')):(item.resolution||'');
      const verification=typeof item.verification==='object'?(item.verification.status||'unverified'):(item.verification||'unverified');
      const sourceMode=item.source_mode||item.source_type;
      const metadata=[sourceModeText(sourceMode),item.version,item.quantity,item.unit,resolution,crs,verification].filter(Boolean).join(' · ');
      const trust=item.trust||{},audit=item.source_audit||{},reasons=(trust.reasons||[]).join(', ')||'无';
      const actions='<div class="button-row"><button class="secondary compact" data-verify-source="'+escapeHtml(item.id)+'" '+(item.path?'':'disabled')+'>验证数据源</button>'
        +(item.id==='reference_landing_sites'||item.id==='reference_routes'?'<button class="secondary compact" data-confirm-crs="'+escapeHtml(item.id)+'">确认 CRS</button>':'')
        +(item.id==='reference_routes'?'<button class="secondary compact" data-preview-routes>预览并导入航线</button>':'')
        +(item.id==='airspace'?'<button class="secondary compact" data-edit-airspace-policy>编辑 AirspacePolicy</button>':'')+'</div>';
      row.innerHTML='<div><strong>'+escapeHtml(item.label)+'</strong><span class="health-badge health-'+item.status+'">'+healthLabel(item.status)+'</span></div><p>'+escapeHtml(item.message)+'</p><small>'+escapeHtml(item.category)+' · '+escapeHtml(item.formats)+(item.required?' · 基础运行必需':' · 可选')+'</small>'+(metadata?'<p>'+escapeHtml(metadata)+'</p>':'')
        +'<p><b>数据可信度/审计</b> · configured '+escapeHtml(trust.configured||'—')+' · identity '+escapeHtml(trust.identity||'—')+' · schema '+escapeHtml(trust.schema||'—')+' · CRS '+escapeHtml(trust.crs||'—')+' · geometry '+escapeHtml(trust.geometry||'—')+' · version '+escapeHtml(trust.version||'—')+' · overall '+escapeHtml(trust.overall||'—')+'</p><small>source_id '+escapeHtml(audit.source_id||'—')+' · reasons '+escapeHtml(reasons)+'</small>'+actions;list.append(row);
    }
    const parameters=$('parameterList');parameters.replaceChildren();const names={vertical_clearance_m:'垂直净空裕度',primary_spacing_factor:'主站间距系数',co_location_search_radius_m:'共址搜索半径'};
    for(const [key,value] of Object.entries(data.defaults?.engineering_parameters||{})){const row=document.createElement('div');row.className='parameter-row';row.textContent=(names[key]||key)+'：'+value.value+(key.endsWith('_m')?' m':'')+' · '+value.source;parameters.append(row);}
  }
const payload=()=>({
  basemap:$('basemapPath').value,
  population:$('populationPath').value,
  terrain:$('terrainPath').value,
  terrain_dtm:$('terrain_dtmPath').value,
  buildings:$('buildingsPath').value,
  building_grid:$('building_gridPath').value,
  reference_landing_sites:$('reference_landing_sitesPath').value,
  reference_routes:$('reference_routesPath').value
});
  function bind(){
    const openSettings=()=>{$('settingsError').textContent='';$('settings').showModal();};
    $('settingsBtn').onclick=$('connection').onclick=openSettings;$('closeSettings').onclick=()=>$('settings').close();
    actionButton('validateSources',async()=>{const data=await api('/api/data-sources/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});render(data);$('settingsMessage').textContent='校验完成；尚未切换当前地图';});
    actionButton('applySources',async()=>{const data=await api('/api/data-sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});onApplied(data);$('settingsMessage').textContent='已保存并重新验证数据源';});
    $('checkOnline').onclick=checkOnline;
    document.querySelectorAll('[data-browse]').forEach(button=>button.onclick=()=>openBrowser(button.dataset.browse,$(button.dataset.browse+'Path').value));
    document.addEventListener('click',async event=>{
      const verify=event.target.closest?.('[data-verify-source]');
      if(verify){const role=verify.dataset.verifySource;try{const data=await api('/api/source-audits/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role})});onApplied(data);$('settingsMessage').textContent='数据源 SHA-256 验证完成';}catch(error){$('settingsError').textContent=error.message;}return;}
      const crs=event.target.closest?.('[data-confirm-crs]');
      if(crs){const value=globalThis.prompt?.('输入经证据确认的 CRS（如 EPSG:4326）','')||'';if(!value)return;const evidence=globalThis.prompt?.('输入 CRS 证据说明','')||'';if(!evidence)return;try{const data=await api('/api/reference-crs/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:crs.dataset.confirmCrs,value,source:{type:'user_confirmation'},evidence:[{type:'user_supplied',note:evidence}]})});onApplied(data);}catch(error){$('settingsError').textContent=error.message;}return;}
      if(event.target.closest?.('[data-preview-routes]')){try{const preview=await api('/api/reference-routes/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});onApplied(preview);$('settingsMessage').textContent='航线预览已生成；请在航路设计页核对后确认导入';}catch(error){$('settingsError').textContent=error.message;}return;}
      if(event.target.closest?.('[data-edit-airspace-policy]')){$('settings').close();document.querySelector('[data-step="3"]')?.click();}
    });
    $('closeBrowser').onclick=()=>$('browser').close();$('drives').onclick=()=>browse('');$('parent').onclick=()=>browse(browseParent);$('openFolder').onclick=()=>browse($('folder').value);
    $('selectFile').onclick=()=>{const target=$(browseKind+'Path');if(selectedFile&&target)target.value=selectedFile;$('browser').close();};
  }
  async function checkOnline(){
    const button=$('checkOnline'),result=$('onlineCheckResult');button.disabled=true;button.textContent='检查中…';result.className='online-check-result checking';result.textContent='正在重新请求在线服务，请稍候…';$('settingsMessage').textContent='正在检查在线服务…';
    try{const data=await onlineTiles.checkSources(),lines=(data.results||[]).map(item=>(item.ok?'✓ ':'✕ ')+item.name+'：'+item.message);result.className='online-check-result '+(data.ok?'ok':'bad');result.textContent=lines.join('\n')||data.message||'没有可检查的在线服务';$('settingsMessage').textContent=data.ok?'在线服务重新检查完成：全部正常':'在线服务重新检查完成：存在异常，请查看检查结果';}
    catch(error){result.className='online-check-result bad';result.textContent='检查失败：'+error.message;$('settingsMessage').textContent='在线服务检查失败';}
    finally{button.disabled=false;button.textContent='检查在线服务';}
  }
  function openBrowser(kind,initialPath=''){
    browseKind=kind;const filters={basemap:'文件类型：QGIS 项目（.qgz / .qgs）',buildings:'文件类型：建筑单体 GeoPackage（.gpkg）',building_grid:'文件类型：建筑环境网格 GeoPackage（.gpkg）',population:'文件类型：人口栅格（.tif / .tiff）',terrain:'文件类型：GLO-30 DSM（.tif / .tiff）',terrain_dtm:'文件类型：FABDEM DTM（.tif / .tiff）',reference_landing_sites:'文件类型：参考起降点（.xlsx / .csv；.et 仅提示转换）',reference_routes:'文件类型：参考航线（.csv / .xlsx / .geojson；.et 仅提示转换）',existing_cns:'文件类型：已有 CNS 设施（.json / .csv / .geojson）',candidate_sites:'文件类型：候选站址（.json / .csv / .geojson）',project:'请选择项目数据存储文件夹'};$('fileFilter').textContent=filters[kind]||'请选择文件';$('selectFile').textContent=kind==='project'?'选择当前文件夹':'选择此文件';$('browser').showModal();browse(initialPath);
  }
  async function browse(path){
    $('browseError').textContent='';selectedFile='';$('selectFile').disabled=true;$('chosen').textContent=browseKind==='project'?'请选择项目文件夹':'请选择文件；单击文件后确认';
    try{const data=await api('/api/browse?'+new URLSearchParams({path,kind:browseKind}));$('folder').value=data.path;browseParent=data.parent;$('entries').replaceChildren();if(browseKind==='project'&&data.path){selectedFile=data.path;$('chosen').textContent='当前文件夹：'+data.path;$('selectFile').disabled=false;}
      for(const entry of data.entries){const button=document.createElement('button');button.className='entry';button.textContent=(entry.directory?'📁  ':'▧  ')+entry.name;button.onclick=()=>{if(entry.directory)return browse(entry.path);$('entries').querySelectorAll('button').forEach(item=>item.classList.remove('selected'));button.classList.add('selected');selectedFile=entry.path;$('chosen').textContent=entry.path;$('selectFile').disabled=false;};$('entries').append(button);}if(!data.entries.length&&browseKind!=='project')$('entries').textContent='此目录没有匹配的文件或子目录';
    }catch(error){$('browseError').textContent='无法浏览：'+error.message;}
  }
  return {render,bind,openBrowser};
}
