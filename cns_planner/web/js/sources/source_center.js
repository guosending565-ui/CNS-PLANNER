import {escapeHtml,sourceModeText,statusText} from '../workflow/common.js';

/**
 * Round38-SITE-1：参考数据 CRS 人工确认门禁的**只读披露**内容。
 *
 * 这里是"要向人展示什么证据"，不是"系统替人决定什么"：
 *  - 该常量不参与任何状态写入、失效传播或规划判定，只用于在 GUI 里把裁定依据、
 *    证据文件指纹与裁定自身的未决限制原样摊开给操作人核对；
 *  - `system_value: null` 表示**系统不预先填好取值**：目标 CRS 必须由人明确输入；
 *  - `origin_nature` 是红线文本：必须让人看到"这是项目内部人工工程裁定，
 *    不是数据提供方正式声明"，从而不能把工程解释伪装成数据源声明。
 */
export const CRS_DOSSIERS={
  reference_landing_sites:{
    label:'参考起降点',
    scope:'source_audit_asset',
    origin_nature:'项目内部人工工程裁定（工程解释），不是企业/数据提供方正式声明。'
      +'原始企业 Excel 未提供独立 CRS 说明；EPSG:4490 的取值来自本轮工程统一口径，'
      +'并由工作副本的 source_crs / crs_confirmed 列与 QA 文档记录。',
    source_warning:'裁定产物保存在**工作副本**；当前项目登记的数据源是**原始件**（见下"当前项目登记的数据源"）。'
      +'两者 Sheet1 逐字节相同，故 98 条记录与 reference_site_id 完全一致，坐标未经任何 CRS 换算。',
    system_value:'EPSG:4490',
    system_value_note:'取值框预填 EPSG:4490 仅为**提示**（复用 DATA-1 既有工程裁定口径）；'
      +'它不是系统替人做的决定：必须由人核对依据、核对当前状态并显式勾选三项门禁后才能写入。',
    provenance_recording:'写入时随记录保存：source.type=user_confirmation、source.origin=human_adjudication、'
      +'evidence[].type=user_supplied，并附下面全部 SHA256；禁止写成 source_file_column/source_file_declaration。',
    evidence:[
      {label:'裁定产物（工作副本 Excel）',
       path:'D:\\aaa2026project\\UOM\\舟山\\基础数据\\空域航线起降点\\空域航线起降点\\舟山市低空起降点汇总260305 - 工作副本.xlsx',
       bytes:30380,sha256:'8a548706dba2c65bb46c1153b3e82473fa2d268582cd45e1dde23c6a2e53be24',
       note:'Sheet1 共 98 条数据行；H 列 source_crs=EPSG:4490 ×98/98；I 列 crs_confirmed=TRUE ×98/98。'},
      {label:'裁定依据（CNS 导入 QA 记录）',
       path:'D:\\aaa2026project\\UOM\\舟山\\基础数据\\空域航线起降点\\空域航线起降点\\舟山市低空起降点_CNS导入_QA.md',
       bytes:41386,sha256:'895eede009a529ddc0ef1c9df204cf57cde4555e7ec766da9b47c5c38078af58',
       note:'原文明确：CNS Planner 本轮工程验证人工统一采用 CGCS2000 / EPSG:4490，'
         +'并声明"不得将 EPSG:4490 描述成企业原始文件已声明的 CRS"。'},
      {label:'未改动证明（两份文件 Sheet1 基线）',
       path:'…\\舟山市低空起降点汇总260305.xlsx 与 …_before_coordinate_import.xlsx',
       bytes:22101,sha256:'08f71f14…c39298',
       note:'两份文件的 xl/worksheets/sheet1.xml SHA256 与工作副本一致（前缀形式）；'
         +'裁定只改了 sheet2.xml（CNS_起降点导入 表），未动原始数据表。'},
      {label:'恢复前权威 ProjectState',
       path:'D:\\Projects\\_cns_workspaces\\acceptance\\round37_b0\\CNS-PLANNER\\projects\\current_project.json',
       bytes:287890185,sha256:'51432a8b7a401fa314b723314188da08e9582684d8115247dd2b1017321910fa',
       note:'revision 468；reference_landing_sites 98 条全部 pending_confirmation，'
         +'crs_confirmation_provenance 不存在（证明从未对该 role 执行过确认）。'}
    ],
    pending_limits:[
      '16 条「估：」低置信度坐标；22 条无显式方向、23 条含异常字符 g。',
      '3 条 district 在舟山辖区外（上海市）；3 组坐标完全相同（6 条）保留不去重、49 条标记 possible_duplicate。',
      '23 条 category 为空；QA 全文无署名、无签字，审计意义上属项目内部工程解释。',
      '确认 CRS 只解除"坐标系未定"这一阻塞，**不**等于认定站址精度合格；上述质量标记继续保留。'
    ]
  },
  reference_routes:{
    label:'真实参考航线',
    scope:'source_file_declaration',
    origin_nature:'源文件列声明（source_file_column）；与起降点的工程裁定性质不同，不得混用同一条来源标注。',
    source_warning:'该 role 已由源文件列自动确认，一般无需人工重复确认。',
    system_value:null,
    system_value_note:'若确需人工确认，请自行输入经证据支持的目标 CRS。',
    provenance_recording:'本对话框对 reference_routes 只提供通用录入；来源标注由服务按 role 语义处理。',
    evidence:[],
    pending_limits:[]
  }
};

/**
 * Round38-SITE-1：人工确认门禁的**纯函数**请求体构造器（无 DOM、无副作用）。
 *
 * 把"来源标注红线"固化成可测的契约，避免它退化成数据源文件声明：
 *  - `source.type` 恒为 `user_confirmation`，并显式带 `origin='human_adjudication'`；
 *  - `evidence[].type` 恒为 `user_supplied`，并附裁定证据产物清单（标签 + SHA256）；
 *  - 绝不产生 `source_file_column` / `source_file_declaration` / `source_file_declaration`
 *    一类的"看起来像数据源声明"的标注。
 */
export function buildCrsConfirmPayload({role,value,dossier}){
  const trimmed=String(value||'').trim();
  if(!role)throw new Error('CRS 确认缺少 role');
  if(!trimmed)throw new Error('CRS 确认缺少经证据确认的取值');
  const evidence=(dossier?.evidence||[]).map(item=>(item.label||'')+' '+(item.sha256||'')).join('；');
  const limits=(dossier?.pending_limits||[]).join(' ');
  return {
    role,
    value:trimmed,
    source:{
      type:'user_confirmation',
      origin:'human_adjudication',
      note:'项目内部人工工程裁定：复用 DATA-1 既有 CGCS2000 / EPSG:4490 工程口径；'
        +'不是数据提供方（企业原始文件）的正式声明。依据：工作副本 source_crs/crs_confirmed 列 + '
        +'《舟山市低空起降点_CNS导入_QA.md》。'
    },
    evidence:[{
      type:'user_supplied',
      value:trimmed,
      note:'人工裁定：'+((dossier?.label)||role)+'的工程统一 CRS 口径。证据产物：'+(evidence||'未登记')
        +(limits?(' 已知未决限制：'+limits):'')
    }]
  };
}

export function towerSourceCompatibilityNotice(data){
  const count=Number(data?.workflow?.towers?.count)||0;
  if(!count||data?.paths?.towers)return '';
  return '已导入 '+count+' 个铁塔站址，但原始数据源路径未登记；请重新选择原始铁塔文件。';
}

export function createSourceCenter({$,api,onlineTiles,onApplied,actionButton,onProjectDirectorySelected}){
  let browseKind='basemap',browseParent='',selectedFile='';
  // Round38-SITE-1：CRS 人工确认门禁的会话草稿（只存在于内存，不写 state）。
  let crsRole='',crsSnapshot=null,crsFingerprintNote='';
  const healthLabel=status=>({ready:'正常',warning:'警告',error:'错误',checking:'检查中'}[status]||statusText(status));
  // 统一路径校验结论（后端每次 metadata() 都重新 exists/is_file，不做缓存）。
  const PATH_CHECK_TEXT={
    ok:'存在且是文件',not_configured:'未配置',path_missing:'路径不存在',
    not_a_file:'不是具体文件',unsupported_format:'格式不受支持'
  };
  function pathCheckLine(data,item){
    const items=data.path_checks?.items||{};
    // 先按 source id 精确匹配；共享同一个配置路径的来源（例如空域参考图层复用底图工程）
    // 再按路径回退匹配，避免这里出现任何政策语义的硬编码分支。
    const record=items[item.id]||Object.values(items).find(entry=>entry.path&&item.path&&entry.path===item.path);
    if(!record||!record.path)return '';
    const label=PATH_CHECK_TEXT[record.status]||record.status;
    const detail=record.status==='ok'?(' · '+escapeHtml(record.suffix||'')):(' · '+escapeHtml(record.reason||''));
    return '<p><b>路径校验</b> · '+escapeHtml(label)+detail+'</p>';
  }
  // 建筑来源解析结论：路径存在但"状态异常"通常来自工程里没有可用图层或格式不被接受。
  function buildingSourceLine(data,item){
    if(item.id!=='buildings'&&item.id!=='building_grid')return '';
    const record=(data.building_sources||{})[item.id]||{};
    if(!record.configured_path)return '';
    if(record.ok){
      const via=record.source==='qgis_project'?'QGIS 工程解析':'数据集直接读取';
      const target=record.resolved_path?escapeHtml(String(record.resolved_path).split(/[\\/]/).pop()):'—';
      return '<p><b>已解析</b> · '+escapeHtml(via)+' → '+target
        +(record.layer_name?'（图层 '+escapeHtml(record.layer_name)+'）':'')+'</p>';
    }
    return '<p><b>解析失败</b> · '+escapeHtml(record.reason||record.status||'未知原因')+'</p>';
  }
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
        +(item.id==='reference_landing_sites'||item.id==='reference_routes'?'<button class="secondary compact" data-confirm-crs="'+escapeHtml(item.id)+'" title="打开人工确认门禁：阅读裁定依据、核对当前状态、显式确认后执行一次写入">确认 CRS…</button>':'')
        +(item.id==='reference_routes'?'<button class="secondary compact" data-preview-routes>预览并导入航线</button>':'')
        +(item.id==='towers'?'<button class="secondary compact" data-import-towers '+(item.path?'':'disabled')+'>导入铁塔站址</button>':'')
        +'</div>';
      const towerNotice=item.id==='towers'?towerSourceCompatibilityNotice(data):'';
      row.innerHTML='<div><strong>'+escapeHtml(item.label)+'</strong><span class="health-badge health-'+item.status+'">'+healthLabel(item.status)+'</span></div><p>'+escapeHtml(item.message)+'</p><small>'+escapeHtml(item.category)+' · '+escapeHtml(item.formats)+(item.required?' · 基础运行必需':' · 可选')+'</small>'+(metadata?'<p>'+escapeHtml(metadata)+'</p>':'')
        +'<p><b>数据可信度/审计</b> · configured '+escapeHtml(trust.configured||'—')+' · identity '+escapeHtml(trust.identity||'—')+' · schema '+escapeHtml(trust.schema||'—')+' · CRS '+escapeHtml(trust.crs||'—')+' · geometry '+escapeHtml(trust.geometry||'—')+' · version '+escapeHtml(trust.version||'—')+' · overall '+escapeHtml(trust.overall||'—')+'</p><small>source_id '+escapeHtml(audit.source_id||'—')+' · reasons '+escapeHtml(reasons)+'</small>'
        +pathCheckLine(data,item)+buildingSourceLine(data,item)
        +(towerNotice?'<p class="inline-error">'+escapeHtml(towerNotice)+'</p>':'')
        +actions;list.append(row);
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
  // V1.1：陆域掩膜必须与其它空间来源一样可以显式配置、校验并持久化。
  land_mask:$('land_maskPath').value,
  reference_landing_sites:$('reference_landing_sitesPath').value,
  reference_routes:$('reference_routesPath').value,
  towers:$('towersPath').value
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
      if(verify){const role=verify.dataset.verifySource;try{const freshState=await api('/api/state');onApplied(freshState);await api('/api/source-audits/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role})});const refreshedState=await api('/api/state');onApplied(refreshedState);$('settingsMessage').textContent='数据源 SHA-256 验证完成';}catch(error){$('settingsError').textContent=error.message;}return;}
      if(event.target.closest?.('[data-import-towers]')){try{const freshState=await api('/api/state');onApplied(freshState);const data=await api('/api/towers/import',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});onApplied(data);const towers=data?.workflow?.towers||{};$('settingsMessage').textContent='已导入真实通信铁塔站址 '+(towers.count||0)+' 个（只读参考数据；不进入规划、风险与覆盖计算）';}catch(error){$('settingsError').textContent=error.message;}return;}
      const crs=event.target.closest?.('[data-confirm-crs]');
      if(crs){await openCrsDialog(crs.dataset.confirmCrs);return;}
      if(event.target.closest?.('[data-preview-routes]')){try{const freshState=await api('/api/state');onApplied(freshState);await api('/api/reference-routes/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const refreshedState=await api('/api/state');onApplied(refreshedState);const routes=refreshedState?.workflow?.reference_routes||{},crs=(routes.crs||{}).source_crs||{};$('settingsMessage').textContent=routes.count>0?(crs.confirmed===true?'已按源文件声明的 '+crs.value+' 导入 '+routes.count+' 条真实参考航线（'+(routes.point_count??0)+' 个航路点），无需再次确认':'已导入 '+routes.count+' 条真实参考航线（坐标系待确认）'):'航线预览已生成；请在航路设计页核对后确认导入';}catch(error){$('settingsError').textContent=error.message;}return;}
    });
    $('closeBrowser').onclick=()=>$('browser').close();$('drives').onclick=()=>browse('');$('parent').onclick=()=>browse(browseParent);$('openFolder').onclick=()=>browse($('folder').value);
    $('selectFile').onclick=()=>{
      const target=$(browseKind+'Path');
      const selected=selectedFile||'';
      if(selected&&target)target.value=selected;
      $('browser').close();
      // BUG-PROJECT-OPEN-UX-001：「选择…」只把目录写进输入框（draft），**绝不**切换项目。
      // 选区结果必须回传给壳层，否则 draft 与输入框会不一致，用户根本无法判断
      // "只是选中了目录"还是"已经打开了项目"。
      if(browseKind==='project'&&selected&&typeof onProjectDirectorySelected==='function'){
        try{onProjectDirectorySelected(selected);}catch(_){/* 纯 UI 通知，失败不影响选择结果 */}
      }
    };
    // Round38-SITE-1：CRS 确认门禁的开关只改弹窗可见性；提交是唯一写入入口。
    $('closeCrsConfirm').onclick=()=>$('crsConfirm').close();
    $('crsConfirmCancel').onclick=()=>$('crsConfirm').close();
    $('crsConfirmSubmit').onclick=executeCrsConfirm;
  }
  async function checkOnline(){
    const button=$('checkOnline'),result=$('onlineCheckResult');button.disabled=true;button.textContent='检查中…';result.className='online-check-result checking';result.textContent='正在重新请求在线服务，请稍候…';$('settingsMessage').textContent='正在检查在线服务…';
    try{const data=await onlineTiles.checkSources(),lines=(data.results||[]).map(item=>(item.ok?'✓ ':'✕ ')+item.name+'：'+item.message);result.className='online-check-result '+(data.ok?'ok':'bad');result.textContent=lines.join('\n')||data.message||'没有可检查的在线服务';$('settingsMessage').textContent=data.ok?'在线服务重新检查完成：全部正常':'在线服务重新检查完成：存在异常，请查看检查结果';}
    catch(error){result.className='online-check-result bad';result.textContent='检查失败：'+error.message;$('settingsMessage').textContent='在线服务检查失败';}
    finally{button.disabled=false;button.textContent='检查在线服务';}
  }
  // ---- Round38-SITE-1：CRS 人工确认门禁（只读披露 + 显式勾选 + 单次写入） ----
  // 本段落**不**引入新的状态写入路径：写入只发生在 executeCrsConfirm() 里那一次
  // POST /api/reference-crs/confirm；打开对话框、刷新状态、渲染证据都只读。
  function crsCollectionOf(state,role){
    const workflow=(state||{}).workflow||{},candidate=workflow[role];
    return candidate&&typeof candidate==='object'?candidate:null;
  }
  //: 权威项目文件（本轮裁定的唯一合法写入目标）。仅用于界面提示"当前打开的是哪个项目"。
  const AUTH_PROJECT_FILE='D:\\Projects\\_cns_workspaces\\acceptance\\round37_b0\\CNS-PLANNER\\projects\\current_project.json';
  function crsStateHtml(state,role){
    const collection=crsCollectionOf(state,role)||{},crs=collection.crs||{},source=crs.source_crs||{},representation=crs.representation_crs||{};
    const audit=(workflowAuditOf(state,role)||{});
    const registry=(audit.file_name||audit.local_path_ref||'—')+' · '+(audit.status||'—');
    const items=(collection.items||[]).length;
    const pending=(collection.items||[]).filter(item=>item.crs_status!=='confirmed').length;
    const valueText=value=>(value===null||value===undefined||value==='')?'未设置（null）':String(value);
    const projectFile=String(((state||{}).project_storage||{}).file||'—');
    const onAuthoritative=projectFile.toLowerCase()===AUTH_PROJECT_FILE.toLowerCase();
    return '<table class="crs-facts">'
      +'<tr><th>当前项目文件</th><td class="'+(onAuthoritative?'crs-ok':'crs-warn')+'">'+escapeHtml(projectFile)
        +(onAuthoritative?'（权威项目 ✅）':'（<b>不是权威项目</b> ⚠ 请先在 Step 1 打开权威项目目录再确认）')+'</td></tr>'
      +'<tr><th>role</th><td>'+escapeHtml(role)+'</td></tr>'
      +'<tr><th>记录数</th><td>'+escapeHtml(String(collection.count??items??'—'))+' 条 · 容器 items '+escapeHtml(String(items))+' 条 · 状态 '+escapeHtml(collection.status||'—')+'</td></tr>'
      +'<tr><th>当前来源坐标系</th><td>'+escapeHtml(valueText(source.value))+' · '+escapeHtml(source.status||'—')+' · confirmed '+escapeHtml(String(source.confirmed===true))+'</td></tr>'
      +'<tr><th>表示坐标系</th><td>'+escapeHtml(valueText(representation.value))+' · '+escapeHtml(representation.status||'—')+'</td></tr>'
      +'<tr><th>待确认记录</th><td><b>'+escapeHtml(String(pending))+'</b> / '+escapeHtml(String(items))+'</td></tr>'
      +'<tr><th>登记数据源</th><td>'+escapeHtml(registry)+'</td></tr>'
      +'<tr><th>集合告警</th><td>'+escapeHtml((collection.warnings||[]).join('、')||'无')+'</td></tr>'
      +'</table>';
  }
  function workflowAuditOf(state,role){
    const workflow=(state||{}).workflow||{};
    const audits=workflow.source_audits||{};
    const items=audits.items||audits||{};
    return items[role]||{};
  }
  function crsEvidenceHtml(dossier){
    const rows=(dossier.evidence||[]).map(item=>'<details class="crs-evidence"><summary>'+escapeHtml(item.label)
      +'<span class="crs-evidence-meta">'+escapeHtml(String(item.bytes??'—'))+' B · '+escapeHtml(String(item.sha256||'—').slice(0,16))+'…</span></summary>'
      +'<p class="crs-path">'+escapeHtml(item.path||'—')+'</p>'
      +'<p class="crs-mono">bytes '+escapeHtml(String(item.bytes??'—'))+'<br>SHA256 '+escapeHtml(item.sha256||'—')+'</p>'
      +'<p>'+escapeHtml(item.note||'')+'</p></details>').join('');
    const limits=(dossier.pending_limits||[]).map(item=>'<li>'+escapeHtml(String(item).replace(/\*\*/g,''))+'</li>').join('');
    return '<h3>裁定依据与证据指纹（点击标题展开）</h3>'+rows
      +'<details class="crs-evidence"><summary>裁定自身的未决限制（必须随记录披露）</summary><ul>'+(limits||'<li>无</li>')+'</ul>'
      +'<p>'+escapeHtml(dossier.provenance_recording.replace(/\*\*/g,''))+'</p></details>';
  }
  function crsGateHtml(dossier){
    return '<label><input type="checkbox" id="crsAckNature" checked> 我确认来源性质：项目内部人工工程裁定，不是数据提供方正式声明</label>'
      +'<label><input type="checkbox" id="crsAckEvidence"> 我确认已核对证据产物的字节数与 SHA256（'+escapeHtml(dossier.evidence.map(item=>String(item.bytes)).join(' / '))+'）</label>'
      +'<label><input type="checkbox" id="crsAckWrite"> 我确认本次为一次正式写入：user_confirmation + human_adjudication + user_supplied，不得伪装成数据源文件声明</label>';
  }
  function updateCrsGate(){
    const ack=$('crsAckNature')?.checked===true,evidence=$('crsAckEvidence')?.checked===true,write=$('crsAckWrite')?.checked===true;
    const value=String($('crsValueInput')?.value||'').trim();
    const submit=$('crsConfirmSubmit');
    if(!submit)return;
    const ready=ack&&evidence&&write&&value.length>0;
    submit.disabled=!ready;
    $('crsConfirmMessage').textContent=ready
      ?'门禁已满足：点击下方按钮将执行一次 POST /api/reference-crs/confirm。'
      :'未确认前不写入任何内容：请勾选三项并确认目标 CRS。';
  }
  function renderCrsDialog(){
    const dossier=CRS_DOSSIERS[crsRole]||{label:crsRole,evidence:[],pending_limits:[],origin_nature:'未登记该 role 的裁定档案。',source_warning:'—',system_value_note:'请自行输入经证据支持的目标 CRS。',provenance_recording:'来源标注由服务按 role 语义处理。'};
    $('crsRoleLine').textContent=crsRole+'（'+dossier.label+'） · '+crsFingerprintNote;
    $('crsDossier').innerHTML=crsStateHtml(crsSnapshot,crsRole)
      +'<details class="crs-evidence crs-warn-box"><summary>来源性质与裁定依据要点</summary>'
      +'<p>'+escapeHtml(dossier.origin_nature)+'</p>'
      +'<p>'+escapeHtml(dossier.source_warning.replace(/\*\*/g,''))+'</p>'
      +'<p>'+escapeHtml(dossier.system_value_note)+'</p></details>'
      +crsEvidenceHtml(dossier);
    $('crsGate').innerHTML=crsGateHtml(dossier);
    $('crsValueInput').value=dossier.system_value||'';
    for(const id of ['crsAckNature','crsAckEvidence','crsAckWrite'])if($(id))$(id).onchange=updateCrsGate;
    $('crsValueInput').oninput=updateCrsGate;
    updateCrsGate();
  }
  async function openCrsDialog(role){
    crsRole=role;$('crsConfirmError').textContent='';$('settingsError').textContent='';
    $('crsConfirmMessage').textContent='正在读取当前项目状态…';
    // 先渲染已知的门禁内容并立即打开弹窗：即使状态读取较慢，门禁也不会"点了没反应"；
    // 状态区随后原地填充，不再触发整页重绘与地图重绘（避免确认前无谓卡顿）。
    crsSnapshot=null;crsFingerprintNote='正在读取…';
    renderCrsDialog();
    $('crsConfirm').showModal();
    try{
      const freshState=await api('/api/state');
      crsSnapshot=freshState;
      const revision=(freshState||{}).workflow?.revision??(freshState||{}).project?.revision??(freshState||{}).revision??'未知';
      crsFingerprintNote='权威状态 revision '+revision;
      renderCrsDialog();
    }catch(error){
      crsSnapshot=null;crsFingerprintNote='状态读取失败';
      renderCrsDialog();
      $('crsConfirmError').textContent='读取当前项目状态失败，请先刷新页面重试：'+error.message;
    }
  }
  async function executeCrsConfirm(){
    const value=String($('crsValueInput').value||'').trim();
    const dossier=CRS_DOSSIERS[crsRole]||{};
    if(!value){$('crsConfirmError').textContent='必须先填写经证据确认的目标 CRS 取值。';return;}
    if($('crsAckNature').checked!==true||$('crsAckEvidence').checked!==true||$('crsAckWrite').checked!==true){
      $('crsConfirmError').textContent='三项人工确认门禁必须全部显式勾选。';return;
    }
    const submit=$('crsConfirmSubmit');
    submit.disabled=true;submit.textContent='正在写入…';$('crsConfirmError').textContent='';
    try{
      // 唯一写入路径：一次 POST。来源标注由 buildCrsConfirmPayload 固定为人工裁定，
      // 绝不退化成 source_file_column / source_file_declaration。
      await api('/api/reference-crs/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(buildCrsConfirmPayload({role:crsRole,value,dossier}))});
      const refreshed=await api('/api/state');
      crsSnapshot=refreshed;
      onApplied?.(refreshed);
      const collection=crsCollectionOf(refreshed,crsRole)||{},source=(collection.crs||{}).source_crs||{};
      $('settingsMessage').textContent='CRS 确认已写入：'+crsRole+' → '+(source.value||value)+'（来源 '+(source.source?.type||'user_confirmation')+' / '+(source.source?.origin||'human_adjudication')+'）；本次为单次写入，请勿重复点击。';
      $('crsConfirm').close();
    }catch(error){
      $('crsConfirmError').textContent=error.message;
    }finally{
      submit.disabled=false;submit.textContent='执行一次 CRS 确认写入';
    }
  }
  function openBrowser(kind,initialPath=''){
    browseKind=kind;const filters={basemap:'文件类型：QGIS 项目（.qgz / .qgs）',buildings:'文件类型：建筑单体（.gpkg / .shp / .geojson，或引用建筑图层的 .qgz / .qgs 工程）',building_grid:'文件类型：建筑环境网格（.gpkg / .shp / .geojson，或引用该图层的 .qgz / .qgs 工程）',land_mask:'文件类型：陆域掩膜（.gpkg / .shp / .geojson；用于 land | coastal_uncertain | sea 判定）',population:'文件类型：人口栅格（.tif / .tiff）',terrain:'文件类型：GLO-30 DSM（.tif / .tiff）',terrain_dtm:'文件类型：FABDEM DTM（.tif / .tiff）',reference_landing_sites:'文件类型：参考起降点（.xlsx / .csv；.et 仅提示转换）',reference_routes:'文件类型：参考航线（.csv / .xlsx / .geojson；.et 仅提示转换）',towers:'文件类型：通信铁塔站址（.xlsx / .csv / .geojson）',existing_cns:'文件类型：已有 CNS 设施（.json / .csv / .geojson）',candidate_sites:'文件类型：候选站址（.json / .csv / .geojson）',project:'请选择项目数据存储文件夹'};$('fileFilter').textContent=filters[kind]||'请选择文件';$('selectFile').textContent=kind==='project'?'选择当前文件夹':'选择此文件';$('browser').showModal();browse(initialPath);
  }
  async function browse(path){
    $('browseError').textContent='';selectedFile='';$('selectFile').disabled=true;$('chosen').textContent=browseKind==='project'?'请选择项目文件夹':'请选择文件；单击文件后确认';
    try{const data=await api('/api/browse?'+new URLSearchParams({path,kind:browseKind}));$('folder').value=data.path;browseParent=data.parent;$('entries').replaceChildren();if(browseKind==='project'&&data.path){selectedFile=data.path;$('chosen').textContent='当前文件夹：'+data.path;$('selectFile').disabled=false;}
      for(const entry of data.entries){const button=document.createElement('button');button.className='entry';button.textContent=(entry.directory?'📁  ':'▧  ')+entry.name;button.onclick=()=>{if(entry.directory)return browse(entry.path);$('entries').querySelectorAll('button').forEach(item=>item.classList.remove('selected'));button.classList.add('selected');selectedFile=entry.path;$('chosen').textContent=entry.path;$('selectFile').disabled=false;};$('entries').append(button);}if(!data.entries.length&&browseKind!=='project')$('entries').textContent='此目录没有匹配的文件或子目录';
    }catch(error){$('browseError').textContent='无法浏览：'+error.message;}
  }
  return {render,bind,openBrowser};
}
