import {escapeHtml,statusText} from '../workflow/common.js';

export function createSourceCenter({$,api,onlineTiles,onApplied,actionButton}){
  let browseKind='basemap',browseParent='',selectedFile='';
  const healthLabel=status=>({ready:'正常',warning:'警告',error:'错误',checking:'检查中'}[status]||statusText(status));
  function render(data){
    const health=data.data_health||{status:'checking',label:'正在检查数据源',items:[]},connection=$('connection');
    connection.className='status status-'+health.status;connection.innerHTML='<i class="dot"></i><span>'+health.label+'</span>';connection.title='打开统一数据源设置查看检查明细';
    const summary=$('healthSummary');summary.className='health-summary health-'+health.status;summary.textContent=health.label+' · '+health.stage+' 文件级检查；工作区覆盖在第 02 步检查';
    const list=$('sourceHealthList');list.replaceChildren();
    for(const item of health.items){
      const row=document.createElement('article');row.className='source-health-item';row.dataset.sourceId=item.id;
      const crs=typeof item.crs==='object'?[item.crs.horizontal,item.crs.vertical].filter(Boolean).join(' / '):(item.crs||'');
      const resolution=typeof item.resolution==='object'?(item.resolution.nominal||[item.resolution.angular_value,item.resolution.angular_unit].filter(value=>value!==undefined).join(' ')):(item.resolution||'');
      const verification=typeof item.verification==='object'?(item.verification.status||'unverified'):(item.verification||'unverified');
      const metadata=[item.source_mode||item.source_type,item.version,item.quantity,item.unit,resolution,crs,verification].filter(Boolean).join(' · ');
      row.innerHTML='<div><strong>'+escapeHtml(item.label)+'</strong><span class="health-badge health-'+item.status+'">'+healthLabel(item.status)+'</span></div><p>'+escapeHtml(item.message)+'</p><small>'+escapeHtml(item.category)+' · '+escapeHtml(item.formats)+(item.required?' · P1 必需':' · 后续/可选')+'</small>'+(metadata?'<p>'+escapeHtml(metadata)+'</p>':'');list.append(row);
    }
    const parameters=$('parameterList');parameters.replaceChildren();const names={vertical_clearance_m:'垂直净空裕度',primary_spacing_factor:'主站间距系数',co_location_search_radius_m:'共址搜索半径'};
    for(const [key,value] of Object.entries(data.defaults?.engineering_parameters||{})){const row=document.createElement('div');row.className='parameter-row';row.textContent=(names[key]||key)+'：'+value.value+(key.endsWith('_m')?' m':'')+' · '+value.source;parameters.append(row);}
  }
  const payload=()=>({basemap:$('basemapPath').value,population:$('populationPath').value,terrain:$('terrainPath').value});
  function bind(){
    const openSettings=()=>{$('settingsError').textContent='';$('settings').showModal();};
    $('settingsBtn').onclick=$('connection').onclick=openSettings;$('closeSettings').onclick=()=>$('settings').close();
    actionButton('validateSources',async()=>{const data=await api('/api/data-sources/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});render(data);$('settingsMessage').textContent='校验完成；尚未切换当前地图';});
    actionButton('applySources',async()=>{const data=await api('/api/data-sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});onApplied(data);$('settingsMessage').textContent='已保存并重新验证数据源';});
    $('checkOnline').onclick=checkOnline;
    document.querySelectorAll('[data-browse]').forEach(button=>button.onclick=()=>openBrowser(button.dataset.browse,$(button.dataset.browse+'Path').value));
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
    browseKind=kind;const filters={basemap:'文件类型：QGIS 项目（.qgz / .qgs）',population:'文件类型：人口栅格（.tif / .tiff）',terrain:'文件类型：地形 DEM（.tif / .tiff）',existing_cns:'文件类型：已有 CNS 设施（.json / .csv / .geojson）',candidate_sites:'文件类型：候选站址（.json / .csv / .geojson）',project:'请选择项目数据存储文件夹'};$('fileFilter').textContent=filters[kind]||'请选择文件';$('selectFile').textContent=kind==='project'?'选择当前文件夹':'选择此文件';$('browser').showModal();browse(initialPath);
  }
  async function browse(path){
    $('browseError').textContent='';selectedFile='';$('selectFile').disabled=true;$('chosen').textContent=browseKind==='project'?'请选择项目文件夹':'请选择文件；单击文件后确认';
    try{const data=await api('/api/browse?'+new URLSearchParams({path,kind:browseKind}));$('folder').value=data.path;browseParent=data.parent;$('entries').replaceChildren();if(browseKind==='project'&&data.path){selectedFile=data.path;$('chosen').textContent='当前文件夹：'+data.path;$('selectFile').disabled=false;}
      for(const entry of data.entries){const button=document.createElement('button');button.className='entry';button.textContent=(entry.directory?'📁  ':'▧  ')+entry.name;button.onclick=()=>{if(entry.directory)return browse(entry.path);$('entries').querySelectorAll('button').forEach(item=>item.classList.remove('selected'));button.classList.add('selected');selectedFile=entry.path;$('chosen').textContent=entry.path;$('selectFile').disabled=false;};$('entries').append(button);}if(!data.entries.length&&browseKind!=='project')$('entries').textContent='此目录没有匹配的文件或子目录';
    }catch(error){$('browseError').textContent='无法浏览：'+error.message;}
  }
  return {render,bind,openBrowser};
}
