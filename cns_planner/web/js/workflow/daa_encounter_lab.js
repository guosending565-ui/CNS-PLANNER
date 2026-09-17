import {escapeHtml,statusBadge} from './common.js';

const finite=value=>Number.isFinite(Number(value));
const fmt=(value,unit='')=>finite(value)?Number(value).toFixed(1)+' '+unit:'—';
const values=object=>Object.values(object||{});

function planGeometry(result){
  const tracks=result.maneuvered_tracks?.length?[result.maneuvered_tracks[0],...(result.tracks||[]).filter(item=>item.role==='intruder')]:result.tracks||[];
  const points=tracks.flatMap(track=>track.samples||[]).filter(item=>finite(item.lon)&&finite(item.lat));
  if(!points.length)return {tracks:[],cpa:null,minTime:0,maxTime:0};
  const lons=points.map(p=>Number(p.lon)),lats=points.map(p=>Number(p.lat));
  const minLon=Math.min(...lons),maxLon=Math.max(...lons),minLat=Math.min(...lats),maxLat=Math.max(...lats),dx=Math.max(1e-9,maxLon-minLon),dy=Math.max(1e-9,maxLat-minLat);
  const xy=p=>[20+360*(Number(p.lon)-minLon)/dx,220-200*(Number(p.lat)-minLat)/dy];
  const converted=tracks.map(track=>({...track,screen:(track.samples||[]).map(sample=>({sample,xy:xy(sample)}))}));
  const cpa=result.geometry?.cpa_position,cpaPoint=cpa&&finite(cpa.lon)&&finite(cpa.lat)?xy(cpa):null;
  const times=points.map(p=>Number(p.time_s)).filter(Number.isFinite);
  return {tracks:converted,cpa:cpaPoint,minTime:Math.min(...times),maxTime:Math.max(...times),project:xy};
}

function trackSvg(result){
  const plan=planGeometry(result);
  if(!plan.tracks.length)return '<div class="empty-note">尚无可绘制的 confirmed EGM2008 encounter tracks。</div>';
  const paths=plan.tracks.map((track,index)=>'<polyline class="daa-track daa-'+escapeHtml(track.role||index)+'" points="'+track.screen.map(p=>p.xy.join(',')).join(' ')+'" fill="none" stroke="'+(track.role==='ownship'?'#20c997':'#ff6b6b')+'" stroke-width="3"/>' ).join('');
  const cpa=plan.cpa?'<path class="daa-cpa" d="M'+(plan.cpa[0]-6)+' '+plan.cpa[1]+'h12M'+plan.cpa[0]+' '+(plan.cpa[1]-6)+'v12" stroke="#ffd43b" stroke-width="3"/>':'';
  return '<svg id="daaPlanView" viewBox="0 0 400 240" role="img" aria-label="Encounter track plan view" style="width:100%;background:#102238;border-radius:6px">'+paths+cpa+'<circle id="daaOwnshipMarker" r="6" fill="#20c997"/><circle id="daaIntruderMarker" r="6" fill="#ff6b6b"/></svg>'+
    '<div class="form-grid"><button class="secondary" id="daaPlay">播放</button><label>Simulation time <input id="daaTime" type="range" min="'+plan.minTime+'" max="'+plan.maxTime+'" step="0.1" value="'+plan.minTime+'"></label></div><div id="daaFrame" class="flow-summary"></div>';
}

export function encounterFrame(result,time){
  const interpolate=track=>{
    const samples=track?.samples||[];if(!samples.length)return null;
    if(time<=samples[0].time_s)return samples[0];if(time>=samples.at(-1).time_s)return samples.at(-1);
    for(let i=0;i<samples.length-1;i++){const a=samples[i],b=samples[i+1];if(a.time_s<=time&&time<=b.time_s){const f=(time-a.time_s)/(b.time_s-a.time_s);return {time_s:time,lon:a.lon+f*(b.lon-a.lon),lat:a.lat+f*(b.lat-a.lat),altitude_egm2008_m:a.altitude_egm2008_m+f*(b.altitude_egm2008_m-a.altitude_egm2008_m)};}}
    return null;
  };
  const own=(result.maneuvered_tracks||[])[0]||(result.tracks||[]).find(item=>item.role==='ownship'),intruder=(result.tracks||[]).find(item=>item.role==='intruder');
  const transitions=result.state_machine?.transitions||[],state=transitions.filter(item=>Number(item.time_s)<=time).at(-1)?.to_state||'NO_TRAFFIC';
  const ownship=interpolate(own),traffic=interpolate(intruder),separation={horizontal_m:null,vertical_m:null,slant_m:null};
  if(ownship&&traffic){const radius=6371008.8,lat=(Number(ownship.lat)+Number(traffic.lat))/2*Math.PI/180,dx=(Number(ownship.lon)-Number(traffic.lon))*Math.PI/180*radius*Math.cos(lat),dy=(Number(ownship.lat)-Number(traffic.lat))*Math.PI/180*radius,dz=Math.abs(Number(ownship.altitude_egm2008_m)-Number(traffic.altitude_egm2008_m));separation.horizontal_m=Math.hypot(dx,dy);separation.vertical_m=dz;separation.slant_m=Math.hypot(separation.horizontal_m,dz);}
  const serviceState=code=>{const item=result.cns_gating?.[code]||{};return (item.intervals||[]).find(interval=>Number(interval.start_time_s)<=time&&time<Number(interval.end_time_s))?.service_state||item.state||'unknown';};
  return {ownship,intruder:traffic,separation,state,cns:{C:serviceState('C'),N:serviceState('N'),S:serviceState('S')}};
}

export function renderDaaEncounterLab(flow){
  const timing=flow.operational_timing||{},result=flow.encounter_3d_assessment||{},lab=timing.encounter_lab||{};
  const tracks=values(timing.encounter_tracks),policy=values(timing.encounter_policies)[0]||{},capability=values(timing.maneuver_capability_profiles)[0]||{},command=values(timing.maneuver_commands)[0]||{};
  const geometry=result.geometry||{},gating=result.cns_gating||{},transitions=result.state_machine?.transitions||[];
  const timeline=transitions.map(item=>'<li><b>'+fmt(item.time_s,'s')+'</b> '+escapeHtml(item.from_state)+' → '+escapeHtml(item.to_state)+'<br><small>'+escapeHtml(item.reason)+'</small></li>').join('')||'<li>尚无 transition</li>';
  const routeOptions='<option value=""></option>'+(flow.service_timeline?.routes||[]).map(item=>'<option value="'+escapeHtml(item.route_id)+'" '+(String(item.route_id)===String(lab.service_route_id)?'selected':'')+'>'+escapeHtml(item.route_id)+'</option>').join('');
  const budgetOptions='<option value=""></option>'+values(timing.response_time_budgets).map(item=>'<option value="'+escapeHtml(item.budget_id)+'" '+(String(item.budget_id)===String(lab.budget_id)?'selected':'')+'>'+escapeHtml(item.budget_id)+'</option>').join('');
  return '<h3>DAA Encounter Lab '+statusBadge(result.status||'not_calculated')+'</h3>'+
    '<div class="demo-note"><strong>engineering simulation / regulatory well-clear not evaluated</strong>；ServiceState ≠ EncounterEvent ≠ SafetyEvent ≠ UnacceptableEvent。不会创建法规 hazard zone。</div>'+
    '<label>EncounterTrack JSON（EGM2008）<textarea id="daaTracks" rows="10">'+escapeHtml(JSON.stringify(tracks,null,2))+'</textarea></label>'+
    '<label>EncounterPolicy JSON（必须显式 confirmed）<textarea id="daaPolicy" rows="7">'+escapeHtml(JSON.stringify(policy,null,2))+'</textarea></label>'+
    '<label>ManeuverCapabilityProfile JSON<textarea id="daaCapability" rows="7">'+escapeHtml(JSON.stringify(capability,null,2))+'</textarea></label>'+
    '<label>ManeuverCommand JSON（只模拟给定 command）<textarea id="daaCommand" rows="7">'+escapeHtml(JSON.stringify(command,null,2))+'</textarea></label>'+
    '<div class="form-grid"><label>Service timeline route<select id="daaServiceRoute">'+routeOptions+'</select></label><label>Protection budget<select id="daaBudget">'+budgetOptions+'</select></label></div>'+
    '<div class="button-row"><button class="secondary" id="saveDaaEncounter">保存 Encounter 输入</button><button class="primary" id="evaluateDaaEncounter">运行 3D Encounter</button></div>'+
    '<div class="flow-summary"><b>当前状态 '+escapeHtml(result.state_machine?.current_state||'NO_TRAFFIC')+'</b><br>Horizontal '+fmt(geometry.current_horizontal_separation_m,'m')+' · Vertical '+fmt(geometry.current_vertical_separation_m,'m')+' · Slant '+fmt(geometry.current_slant_separation_m,'m')+'<br>Time-to-CPA '+fmt(geometry.time_to_horizontal_cpa_s,'s')+' · projected horizontal CPA '+fmt(geometry.horizontal_cpa_m,'m')+' · vertical@CPA '+fmt(geometry.vertical_separation_at_cpa_m,'m')+'<br>C '+escapeHtml(gating.C?.state||'unknown')+' · N '+escapeHtml(gating.N?.state||'unknown')+' · S '+escapeHtml(gating.S?.state||'unknown')+' · ownship confidence '+escapeHtml(gating.ownship_state_confidence||'unknown')+'</div>'+
    trackSvg(result)+'<h4>DAA transition timeline</h4><ol class="daa-transition-timeline">'+timeline+'</ol>';
}

let playTimer=null;
export function bindDaaEncounterLab(c){
  c.actionButton('saveDaaEncounter',()=>{
    const timing=structuredClone(c.flow().operational_timing||{}),tracks=JSON.parse(c.$('daaTracks').value||'[]'),policy=JSON.parse(c.$('daaPolicy').value||'{}'),capability=JSON.parse(c.$('daaCapability').value||'{}'),command=JSON.parse(c.$('daaCommand').value||'{}');
    timing.encounter_tracks=Object.fromEntries(tracks.filter(item=>item.track_id).map(item=>[item.track_id,item]));
    timing.encounter_policies=policy.policy_id?{[policy.policy_id]:policy}:{};
    timing.maneuver_capability_profiles=capability.capability_id?{[capability.capability_id]:capability}:{};
    timing.maneuver_commands=command.command_id?{[command.command_id]:command}:{};
    timing.encounter_lab={ownship_track_id:tracks.find(item=>item.role==='ownship')?.track_id||'',intruder_track_id:tracks.find(item=>item.role==='intruder')?.track_id||'',policy_id:policy.policy_id||'',capability_id:capability.capability_id||'',command_id:command.command_id||'',service_route_id:c.$('daaServiceRoute').value,budget_id:c.$('daaBudget').value};
    return c.resourceAction('/api/operational-timing',{operational_timing:timing});
  });
  c.actionButton('evaluateDaaEncounter',()=>c.resourceAction('/api/encounter-3d/evaluate',{}));
  const slider=c.$('daaTime'),svg=c.$('daaPlanView');if(!slider||!svg)return;
  const result=c.flow().encounter_3d_assessment||{},plan=planGeometry(result);
  const update=()=>{const frame=encounterFrame(result,Number(slider.value));for(const [id,point] of [['daaOwnshipMarker',frame.ownship],['daaIntruderMarker',frame.intruder]]){const marker=c.$(id);if(!marker||!point||!plan.project)continue;const xy=plan.project(point);marker.setAttribute('cx',xy[0]);marker.setAttribute('cy',xy[1]);}c.$('daaFrame').textContent='t='+fmt(slider.value,'s')+' · '+frame.state+' · C/N/S '+frame.cns.C+'/'+frame.cns.N+'/'+frame.cns.S+' · H/V/Slant '+fmt(frame.separation.horizontal_m,'m')+'/'+fmt(frame.separation.vertical_m,'m')+'/'+fmt(frame.separation.slant_m,'m')+' · ownship '+(frame.ownship?Number(frame.ownship.lon).toFixed(6)+','+Number(frame.ownship.lat).toFixed(6)+' / '+fmt(frame.ownship.altitude_egm2008_m,'m EGM2008'):'—')+' · intruder '+(frame.intruder?Number(frame.intruder.lon).toFixed(6)+','+Number(frame.intruder.lat).toFixed(6)+' / '+fmt(frame.intruder.altitude_egm2008_m,'m EGM2008'):'—');};
  slider.oninput=update;update();
  c.$('daaPlay').onclick=()=>{if(playTimer){clearInterval(playTimer);playTimer=null;c.$('daaPlay').textContent='播放';return;}c.$('daaPlay').textContent='暂停';playTimer=setInterval(()=>{const next=Number(slider.value)+Number(slider.step||.1);slider.value=next>Number(slider.max)?slider.min:next;update();},100);};
}
