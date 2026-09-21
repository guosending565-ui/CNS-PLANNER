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
