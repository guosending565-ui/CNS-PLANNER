export function createApiClient(token=()=>""){
  return async function api(url,options={}){
    const response=await fetch(url,{...options,headers:{'X-CNS-Token':token()||'',...options.headers}});
    const type=response.headers.get('content-type')||'';
    const data=type.includes('json')?await response.json():await response.blob();
    if(!response.ok)throw Error(data.error||'请求失败');
    return data;
  };
}
