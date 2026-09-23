export function createApiClient(token=()=>"",revision=()=>null){
  let sequence=0,writeQueue=Promise.resolve(),knownRevision=null;
  const clientId=globalThis.crypto?.randomUUID?.()||('client-'+Math.random().toString(36).slice(2));
  async function execute(url,options,requestId){
    const method=String(options.method||'GET').toUpperCase(),headers={'X-CNS-Token':token()||'',...options.headers};
    if(method==='POST'){
      const current=knownRevision??revision();
      if(!Number.isInteger(current))throw Error('当前 workflow revision 不可用，请刷新项目状态');
      headers['X-CNS-Revision']=String(current);
      headers['X-CNS-Request-Id']=requestId;
    }
    const response=await fetch(url,{...options,headers});
    const type=response.headers.get('content-type')||'';
    const data=type.includes('json')?await response.json():await response.blob();
    const responseRevision=Number(response.headers.get('x-cns-revision'));
    if(Number.isInteger(responseRevision))knownRevision=responseRevision;
    else if(Number.isInteger(data?.workflow?.revision))knownRevision=data.workflow.revision;
    else if(url==='/api/workflow'&&Number.isInteger(data?.revision))knownRevision=data.revision;
    if(!response.ok){const error=Error(data.error||'请求失败');error.status=response.status;error.revision=data.revision;throw error;}
    return data;
  }
  return function api(url,options={}){
    if(String(options.method||'GET').toUpperCase()!=='POST')return execute(url,options,'');
    const requestId=clientId+':'+(++sequence),run=()=>execute(url,options,requestId);
    const pending=writeQueue.then(run,run);writeQueue=pending.catch(()=>{});return pending;
  };
}

/**
 * 局部 mutation 的唯一安全顺序（BUG-STEP03-RESOURCEACTION-001）。
 *
 * 一部分 POST 端点（layered validation evaluate-real、operational adoption
 * preview/apply/revoke、V3 policy/refinement/validation、Route3DProfile evaluate/delete …）
 * 只返回**该资源自己的局部对象**（validation collection / projection / preview / readiness /
 * 被删除的记录），**不是**完整 workflow。把这种 response 直接赋给全局 flow 会让
 * ``flow.project`` / ``flow.workspace`` / ``flow.grid`` 立刻消失，界面随即抛
 * ``Cannot read properties of undefined (reading 'name')``（后端数据其实没丢）。
 *
 * 这里把顺序固定为：POST → 保留 POST response → 重新读取完整 workflow →
 * ``applyWorkflow(full)`` → 返回 POST response。判定依据是**端点契约**，不做
 * "猜 response 是否完整"的启发式判断。
 */
export function createResourceMutationAndRefresh({post,refresh,apply}){
  if(typeof post!=='function'||typeof refresh!=='function'||typeof apply!=='function')
    throw new Error('resourceMutationAndRefresh 需要 post / refresh / apply 三个依赖');
  return async function resourceMutationAndRefresh(path,payload={}){
    // POST 的响应只作为调用方的返回值：它绝不进入全局 flow。
    const data=await post(path,payload);
    // 唯一允许写入全局 flow 的路径：完整 workflow 快照。
    await apply(await refresh());
    return data;
  };
}
