import {escapeHtml} from '../workflow/common.js';

export const PROVIDER_FACTS = {
  device_catalog: '设备目录与设备参数；不等于实测覆盖，不得将厂商参考距离当规划半径。',
  coverage_3d: '三维传播模型覆盖结果；需要上游输入、设施、模型与结果指纹均为 current。',
  cns_service_capability: '给定模型与设施下的 CNS 服务能力；需要覆盖、需求及能力评估均为 current。'
};

export function createE2eConfirmation({api,onApplied}) {
  let opening=false;
  const drafts=new Map();
  async function open(role) {
    if(opening)return;
    opening=true;
    try {
    const state = await api('/api/state');
    let flow = state.workflow || {};
    const provider = role === 'communication_provider';
    const field = provider ? await api('/api/communication-planning-field') : null;
    let audit = flow.source_audits?.items?.[role] || {};
    let options = field?.provider_options || [];
    let reviewedFile=state.project_storage?.file||'',submitting=false,committed=false,closed=false;
    const dialog = document.createElement('dialog');
    dialog.style.maxWidth = '780px';
    dialog.innerHTML = '<h3>'+ (provider?'通信规划 provider 选择':'建筑数据 CRS 人工确认') +'</h3>'
      +'<p>打开时项目快照：'+escapeHtml(state.project_storage?.file || '未知')+' · revision '+escapeHtml(String(flow.revision))+'</p>'
      +(provider ? '<p>当前 provider：'+escapeHtml(field.provider || '未选择')+'；状态：'+escapeHtml(field.status)+'</p>'
        +'<p>本操作只保存选择意向，不生成逐格通信事实、不改变路径或成本、不升级历史 stale 结果。正式可用还需要经过确认的逐格映射。</p>'
        +'<select data-value><option value="">请主动选择 provider</option>'+options.map(o=>'<option value="'+escapeHtml(o.id)+'">'+escapeHtml(o.id+' · '+o.status)+'</option>').join('')+'</select>'
        +Object.entries(PROVIDER_FACTS).map(([id,text])=>'<p>'+escapeHtml(id+'：'+text)+'</p>').join('')
        : '<p>数据源：'+escapeHtml(role+' · '+(audit.file_name || '未登记'))+'</p>'
          +'<p>声明 CRS：'+escapeHtml(audit.declared_crs || '未知')+'；审计：'+escapeHtml(audit.status || '未知')+'</p>'
          +'<p>SHA256：'+escapeHtml(audit.sha256 || '未验证')+'<br>指纹：'+escapeHtml(audit.version_fingerprint || '未知')+'</p>'
          +'<pre>'+escapeHtml(JSON.stringify({extent:audit.extent,provenance:audit.provenance,geometry_health:audit.geometry_health},null,2))+'</pre>'
          +'<p>坐标范围和文件声明不能证明真实大地基准。本入口仅确认声明，不执行坐标转换；不确认高度、拓扑或精度。审计过期时须先验证数据源。</p>'
          +'<label>输入经证据支持的声明 CRS <input data-value placeholder="请人工填写"></label>')
      +'<p><label>人工依据与限制 <textarea data-evidence></textarea></label></p>'
      +'<p><label><input type="checkbox" data-ack> 我已核对当前项目、来源、指纹与限制，明确授权本次写入。</label></p>'
      +'<p data-identity></p><p data-error role="alert" style="color:#b42318;white-space:pre-wrap"></p><button data-submit disabled>确认写入一次</button> <button data-close>取消</button>';
    document.body.append(dialog);
    const q=s=>dialog.querySelector(s);
    const draft=drafts.get(role);
    if(draft){q('[data-value]').value=draft.value;q('[data-evidence]').value=draft.evidence;}
    const describe=()=>{q('[data-identity]').textContent='本次审阅：'+reviewedFile+' · revision '+flow.revision+' · SHA256（最近验证值） '+(audit.sha256||'未验证')+' · 指纹 '+(audit.version_fingerprint||'未登记')+' · '+(audit.status||'')+' · 声明 CRS '+(audit.declared_crs||'未知')+' · 文件签名 '+(audit.size_bytes??'未知')+'/'+(audit.mtime_ns??'未知');};
    describe();
    const update=()=>{q('[data-submit]').disabled=submitting||committed||!(q('[data-ack]').checked && q('[data-value]').value.trim() && q('[data-evidence]').value.trim())||(!provider&&audit.status!=='verified');};
    dialog.addEventListener('input', update);
    q('[data-close]').onclick=()=>dialog.close();
    dialog.addEventListener('close',()=>{
      closed=true;opening=false;
      if(committed)drafts.delete(role);
      else drafts.set(role,{value:q('[data-value]').value,evidence:q('[data-evidence]').value});
      dialog.remove();
    });
    q('[data-submit]').onclick=async()=>{
      if(submitting||committed||q('[data-submit]').disabled)return;
      submitting=true;update();q('[data-error]').textContent='正在复核项目版本与源身份（文件签名；提交时仍会校验 SHA256）…';
      try {
        const value=q('[data-value]').value.trim();
        const fresh=provider?await api('/api/state'):await api('/api/source-audits/identity?role='+encodeURIComponent(role));
        const freshFlow=provider?(fresh.workflow||{}):{revision:fresh.revision};
        const freshField=provider?await api('/api/communication-planning-field'):null;
        const nextAudit=provider?audit:(fresh.audit||{});
        const nextOptions=provider?(freshField?.provider_options||[]):options;
        const oldFp=provider?options.find(o=>o.id===value)?.source_fingerprint:audit.version_fingerprint;
        const nextFp=provider?nextOptions.find(o=>o.id===value)?.source_fingerprint:nextAudit.version_fingerprint;
        const nextFile=fresh.project_storage?.file||'';
        if(closed)return;
        if(flow.revision!==freshFlow.revision||oldFp!==nextFp||reviewedFile!==nextFile||(!provider&&audit.status!==nextAudit.status)){
          const reason=oldFp!==nextFp||audit.sha256!==nextAudit.sha256?'数据源身份变化':'其它项目状态或活动项目变化';
          q('[data-error]').textContent=reason+'：revision '+flow.revision+' → '+freshFlow.revision+'；指纹 '+(oldFp||'无')+' → '+(nextFp||'无')+'；SHA256 '+(audit.sha256||'无')+' → '+(nextAudit.sha256||'无')+'。\n已保留 CRS 与依据。请审阅上方更新后的身份，重新勾选授权后再次提交。'+(!provider&&nextAudit.status!=='verified'?' 数据源已过期，须先完成验证。':'');
          flow=freshFlow;audit=nextAudit;options=nextOptions;reviewedFile=nextFile;q('[data-ack]').checked=false;describe();return;
        }
        const payload={confirmed:true,expected_revision:flow.revision,evidence:[{type:'user_supplied',note:q('[data-evidence]').value.trim()}]};
        if(provider){payload.provider_selection=value;payload.source_fingerprint=options.find(o=>o.id===value)?.source_fingerprint;}
        else {payload.role=role;payload.value=value;payload.source_fingerprint=audit.version_fingerprint;}
        await api(provider?'/api/communication-planning-field':'/api/data-sources/crs/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        committed=true;
        drafts.delete(role);
        if(onApplied)await onApplied(await api('/api/state'));
        dialog.close();opening=false;
      } catch(error) {
        const label=committed?'写入成功，但状态刷新失败':error.status===409?'409 乐观锁冲突':error.status===400&&/revision|指纹|内容变化/.test(error.message)?'400 业务版本冲突':error.status?'业务校验失败':'网络错误';
        q('[data-error]').textContent=label+'：'+error.message+'。填写内容已保留；请核对后重试。';
        q('[data-ack]').checked=false;
      } finally {submitting=false;update();}
    };
    dialog.showModal();
    } catch(error){opening=false;throw error;}
  }
  return {open};
}
