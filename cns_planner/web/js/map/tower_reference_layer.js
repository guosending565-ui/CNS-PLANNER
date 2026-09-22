// =========================================================
// 真实通信铁塔站址的只读交互层（数据侧契约见 cns_planner/domain/towers.py）。
//
// 边界（不越界）：
//  - 只读 currentPlan / flow.towers / flow.tower_obstacle_profiles /
//    flow.tower_colocation_candidates，绝不写入任何业务状态；
//  - 不调用任何 API，不触发保存、失效或重算；
//  - 只展示源文件真实字段、来源行与**已落库的派生事实**，绝不派生 coverage / 频率 /
//    功率 / 容量等通信能力结论——"有站址"不等于"有 CNS 设备"；
//  - 不新建地图状态机：绘制完全由 map/display_layers.js + lod.js +
//    point_clustering.js 决定，本模块只负责 hover 提示与 click 详情。
// =========================================================
import {hitDisplayEntry,hitCnsTowerCandidate} from './display_layers.js';

/** 命中一个铁塔显示条目：聚合点带 count，单点带 anchor.tower。 */
export function hitTowerEntry(plan,point){
  if(!plan)return null;
  return hitDisplayEntry(plan,point,{kind:'towers'});
}

/** hover 简短提示：单点给出"编码 · 名称 · 塔型"，聚合点只给出数量。 */
export function towerHoverTitle(hit){
  if(!hit)return '';
  if(hit.count>1)return '聚合 '+hit.count+' 个通信铁塔站址（点击放大到范围）';
  const tower=(hit.anchor&&hit.anchor.tower)||{};
  return [tower.tower_id,tower.name,tower.site_type].filter(Boolean).join(' · ');
}

function number(value){return typeof value==='number'&&Number.isFinite(value)?value:null;}

/**
 * 塔顶高度信息：只用**已落库的派生事实**，绝不在这里做 elevation+height 的加法。
 * 未解析时明确说明"未解析"，而不是显示一个编出来的数字。
 */
export function towerTopHtml(profile,escapeHtml){
  const safe=typeof escapeHtml==='function'?escapeHtml:value=>String(value??'');
  if(!profile)return '<br><small>规划采用塔顶高程：尚未派生（请在数据源中心执行"铁塔派生"）</small>';
  const top=number(profile.tower_top_orthometric_m);
  const base={ground:'地面塔',rooftop:'楼面塔',unknown:'塔基类型未知'}[profile.base_type]||profile.base_type;
  if(top===null){
    return '<br>规划采用塔顶高程：<b>未解析</b>'
      +'<br><small>'+safe(profile.vertical_status||'unresolved')+' · '+safe(profile.reason||'证据不足')+'</small>';
  }
  return '<br>规划采用塔顶高程：<b>'+top.toFixed(1)+' m EGM2008</b>'
    +'<br><small>'+safe(base)
    +(number(profile.terrain_elevation_m)!==null?' · 地形 '+number(profile.terrain_elevation_m).toFixed(1)+' m':'')
    +(number(profile.building_height_m)!==null?' · 建筑 '+number(profile.building_height_m).toFixed(1)+' m':'')
    +(number(profile.tower_structure_height_m)!==null?' · 塔身 '+number(profile.tower_structure_height_m).toFixed(1)+' m':'')
    +'</small>';
}

/** click 详情：只展示源文件真实字段、来源行与已落库的派生事实。 */
export function towerDetailHtml(tower,escapeHtml,{obstacleProfile=null,colocation=null}={}){
  if(!tower)return '';
  const source=tower.source||{},safe=typeof escapeHtml==='function'?escapeHtml:value=>String(value??'');
  const colocationLine=colocation
    ?'<br>CNS 共塔候选：<b>是</b> · 关联 '+safe(colocation.site_id)
      +'<br><small>宿主 '+safe(((colocation.metadata||{}).host||{}).host_type||'tower')
      +' · '+(colocation.planning_profile||{}).reuse_class+' · '
      +safe((colocation.planning_profile||{}).status||'pending_confirmation')+'</small>'
    :'<br>CNS 共塔候选：否';
  return '<b>通信铁塔站址</b><br>'+safe(tower.tower_id)+' · '+safe(tower.name)
    +'<br>经度 '+tower.longitude+' · 纬度 '+tower.latitude
    +'<br>源数据海拔 '+(tower.elevation_m??'—')+' m · 塔身高度 '+(tower.height_m??'—')+' m'
    +'<br>区域 '+(tower.district?safe(tower.district):'—')+' · 塔型 '+(tower.site_type?safe(tower.site_type):'—')
    +towerTopHtml(obstacleProfile,escapeHtml)
    +colocationLine
    +'<br>来源 '+safe(source.file_name||'—')+(source.sheet?' · '+safe(source.sheet):'')+' 第 '+(source.row??'—')+' 行'
    +'<br><small>只读真实站址位置；不代表 CNS 设备、覆盖能力或可用性</small>';
}

/** 共塔候选详情：说明"这是宿主候选，不是已安装设备"。 */
export function towerColocationDetailHtml(candidate,escapeHtml){
  if(!candidate)return '';
  const safe=typeof escapeHtml==='function'?escapeHtml:value=>String(value??''),metadata=candidate.metadata||{},host=metadata.host||{};
  return '<b>CNS 共塔候选（宿主）</b><br>'+safe(candidate.site_id)
    +'<br>Tower ID '+safe(host.host_tower_id||'—')+' · '+safe(host.host_tower_name||'—')
    +'<br>Tower Type '+safe(host.host_site_type||'—')
    +'<br>经度 '+candidate.coordinate[0]+' · 纬度 '+candidate.coordinate[1]
    +'<br>宿主可用性 位置'+(host.site_position_available?'可用':'未知')+' · 设备挂载'+(host.device_mount_confirmed?'已确认':'未确认')
    +'<br><small>共塔候选只表示"可以作为宿主"，不代表已安装任何 CNS 设备，也不声明覆盖能力</small>';
}

/** 从 flow 取某个塔的只读派生上下文：塔顶障碍物事实 + 对应的 CNS 共塔候选（若有）。 */
export function towerDetailContext(flow,towerId){
  const profiles=(((flow||{}).tower_obstacle_profiles)||{}).items||{};
  const colocation=((((flow||{}).tower_colocation_candidates)||{}).items||[]).find(item=>
    String((((item.metadata||{}).host)||{}).host_tower_id||'')===String(towerId))||null;
  return {obstacleProfile:profiles[towerId]||null,colocation};
}

/**
 * 命中 CNS 共塔候选时填充详情面板、记录"当前高亮的宿主铁塔"并重绘。
 * 返回值只表示"是否命中共塔候选"，调用方据此终止本次 click 的后续处理。
 *
 * 高亮是**纯 UI 状态**（模块内的一个 tower_id），不写项目状态、不发请求、
 * 也不画任何从候选到铁塔的永久连接线。
 */
export function showColocationCandidate({plan,point,info,escapeHtml,canvas,paint}){
  const candidate=hitCnsTowerCandidate(plan,point);
  if(!candidate||!candidate.hostTowerId||!info)return null;
  const rect=canvas.getBoundingClientRect();
  info.innerHTML=towerColocationDetailHtml(candidate.site,escapeHtml);
  info.style.left=Math.max(8,Math.min(point[0]+12,rect.width-440))+'px';
  info.style.top=Math.max(8,point[1]-38)+'px';
  info.hidden=false;
  if(typeof paint==='function')paint();
  return candidate;
}

/** 挂载铁塔交互：hover 写 canvas.title；click 详情由调用方通过 ``detail()`` 触发。 */
export function attachTowerReferenceLayer({canvas,getPlan,isEnabled,getInfo,escapeHtml,getTowerContext,paint}){
  //: 当前被 CNS 共塔候选联动的宿主铁塔（纯 UI 状态，随交互整体替换）。
  let highlightedTowerId=null;
  canvas.addEventListener('mousemove',event=>{
    const plan=getPlan();
    if(!plan||!isEnabled()){if(canvas.title)canvas.title='';return;}
    const rect=canvas.getBoundingClientRect();
    const title=towerHoverTitle(hitTowerEntry(plan,[event.clientX-rect.left,event.clientY-rect.top]));
    if(canvas.title!==title)canvas.title=title;
  });
  /** 命中单点铁塔时填充网格信息面板并返回 true；否则返回 false（调用方继续原流程）。 */
  function detail(clusterTarget,event){
    if(!clusterTarget||clusterTarget.count!==1)return false;
    const tower=clusterTarget.anchor&&clusterTarget.anchor.tower;
    if(!tower)return false;
    const info=typeof getInfo==='function'?getInfo():null;
    if(!info)return false;
    const context=typeof getTowerContext==='function'?(getTowerContext(tower.tower_id)||{}):{};
    const rect=canvas.getBoundingClientRect();
    info.innerHTML=towerDetailHtml(tower,escapeHtml,context);
    info.style.left=Math.max(8,Math.min(event.clientX-rect.left+12,rect.width-440))+'px';
    info.style.top=Math.max(8,event.clientY-rect.top-38)+'px';
    info.hidden=false;
    return true;
  }
  return {
    detail,
    /** 当前高亮的宿主铁塔 id（供显示计划绘制高亮环）。 */
    highlightedTower(){return highlightedTowerId;},
    /**
     * click 优先级中的"共塔候选"一步：命中则显示候选详情并高亮宿主铁塔，返回 true。
     * 未命中时清除上一次高亮（避免高亮残留），并返回 false 让调用方继续原流程。
     */
    candidateClick(event){
      const plan=getPlan();
      if(!plan){return false;}
      const rect=canvas.getBoundingClientRect();
      const found=showColocationCandidate({
        plan,point:[event.clientX-rect.left,event.clientY-rect.top],
        info:typeof getInfo==='function'?getInfo():null,escapeHtml,canvas,
      });
      if(found){highlightedTowerId=found.hostTowerId;return true;}
      if(highlightedTowerId){highlightedTowerId=null;if(typeof paint==='function')paint();}
      return false;
    },
  };
}
