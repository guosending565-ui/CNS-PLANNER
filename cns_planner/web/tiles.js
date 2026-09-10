/* Online basemap tiles load separately from local QGIS overlays. */
class OnlineTiles {
  constructor(redraw, status) {this.redraw=redraw;this.status=status;this.cache=new Map();this.active=new Map();this.pending=[];this.sources=[];this.visible=[];this.failed=new Map();this.enabled=false;}
  configure(sources,revision) {
  this.sources=[...sources].reverse().map(source=>{
    const text=((source.name||'')+' '+(source.browser_url||'')).toLowerCase();

    return {
      ...source,
      isAnnotation:(
        text.includes('注记') ||
        text.includes('标注') ||
        text.includes('annotation') ||
        text.includes('cva_w') ||
        text.includes('cia_w') ||
        text.includes('cva') ||
        text.includes('cia')
      )
    };
  });

  this.revision=revision;
  this.failed.clear();
}
  update(view,w,h,enabled) {
    this.started=performance.now();this.completed=null;
    this.enabled=enabled;this.visible=[];this.pending=[];
    if(enabled&&view){
      for(const source of this.sources){
        const z=Math.max(source.zmin,Math.min(source.zmax,Math.round(Math.log2(156543.03392804097/view.res))));
        const n=2**z,span=40075016.68557849/n,origin=20037508.342789244;
        const west=view.x-w*view.res/2,east=view.x+w*view.res/2,north=view.y+h*view.res/2,south=view.y-h*view.res/2;
        for(let x=Math.max(0,Math.floor((west+origin)/span));x<=Math.min(n-1,Math.floor((east+origin)/span));x++)
          for(let y=Math.max(0,Math.floor((origin-north)/span));y<=Math.min(n-1,Math.floor((origin-south)/span));y++){
            const key=`${this.revision}/${source.id}/${z}/${x}/${y}`;
            const tile={
  key,
  source:source.id,
  browserURL:source.browser_url,
  isAnnotation:!!source.isAnnotation,
  z,
  x,
  y,
  left:x*span-origin,
  top:origin-y*span,
  span
};

this.visible.push(tile);
            if(!this.cache.has(key)&&!this.active.has(key)&&Date.now()-(this.failed.get(key)||0)>15000)this.pending.push(tile);
          }
      }
    }
    const needed=new Set(this.visible.map(t=>t.key));
    this.cached=this.visible.filter(t=>this.cache.has(t.key)).length;
    for(const [key,controller] of this.active)if(!needed.has(key))controller.abort();
    this.pump();
  }
  pump(){
    while(this.active.size<6&&this.pending.length){
      const tile=this.pending.shift(),controller=new AbortController();this.active.set(tile.key,controller);
      const q=new URLSearchParams({source:tile.source,z:tile.z,x:tile.x,y:tile.y,rev:this.revision});
      const download=tile.browserURL ? this.browserImage(tile,controller.signal) : fetch('/api/tile?'+q,{signal:controller.signal}).then(async response=>{
        if(!response.ok)throw Error('底图服务响应失败');return createImageBitmap(await response.blob());
      });
      download.then(bitmap=>{
        this.cache.set(tile.key,bitmap);
        while(this.cache.size>256){const key=this.cache.keys().next().value;this.cache.get(key).close?.();this.cache.delete(key);}
        this.redraw();
      }).catch(exc=>{if(exc.name!=='AbortError')this.failed.set(tile.key,Date.now());})
      .finally(()=>{this.active.delete(tile.key);this.pump();});
    }
    const missing=this.visible.filter(t=>!this.cache.has(t.key)).length;
    const failures=this.visible.filter(t=>this.failed.has(t.key)&&!this.cache.has(t.key)).length;
    if(!missing&&this.completed===null)this.completed=Math.round(performance.now()-this.started);
    this.status(!this.enabled?'':!this.sources.length?'项目中没有可用 XYZ 底图':missing?(failures?'部分在线瓦片暂不可用，本地图层不受影响':`在线底图加载中 · 剩余 ${missing} 块`):`在线底图已加载 · ${this.completed} ms · 缓存 ${this.cached}/${this.visible.length} 块`);
  }
  async checkSources(){
  const results=[];

  for(const source of this.sources){

    if(!source.browser_url){
      results.push({
        name:source.name||'在线服务',
        ok:false,
        message:'没有浏览器访问地址'
      });
      continue;
    }

    const z=Math.max(
      Number(source.zmin||0),
      Math.min(
        10,
        Number(source.zmax||18)
      )
    );

    // 浙江附近 120E, 30N
    const lon=120.0;
    const lat=30.0;

    const n=2**z;

    const x=Math.floor(
      (lon+180)/360*n
    );

    const y=Math.floor(
      (
        1-
        Math.asinh(
          Math.tan(lat*Math.PI/180)
        )/Math.PI
      )/2*n
    );

    const url=source.browser_url
      .replaceAll('{z}',z)
      .replaceAll('{x}',x)
      .replaceAll('{y}',y);

    try{

      await new Promise((resolve,reject)=>{

        const img=new Image();

        const timer=setTimeout(()=>{
          img.src='';
          reject(
            new Error('请求超时')
          );
        },8000);

        img.onload=()=>{
          clearTimeout(timer);
          resolve();
        };

        img.onerror=()=>{
          clearTimeout(timer);
          reject(
            new Error('浏览器瓦片加载失败')
          );
        };

        img.src=url;
      });

      results.push({
        name:source.name||'在线服务',
        ok:true,
        message:'浏览器瓦片加载正常'
      });

    }catch(exc){

      results.push({
        name:source.name||'在线服务',
        ok:false,
        message:exc.message
      });

    }
  }

  return {
    ok:
      results.length>0 &&
      results.every(item=>item.ok),

    results
  };
}
  browserImage(tile,signal){
    return new Promise((resolve,reject)=>{
      const img=new Image();let settled=false;
      const finish=(error)=>{if(settled)return;settled=true;clearTimeout(timer);signal.removeEventListener('abort',abort);img.onload=img.onerror=null;if(error){img.removeAttribute('src');reject(error);}else resolve(img);};
      const abort=()=>finish(new DOMException('视图已切换','AbortError'));
      const timer=setTimeout(()=>finish(Error('在线底图超时')),6000);
      img.onload=()=>finish();img.onerror=()=>finish(Error('在线底图请求失败'));
      signal.addEventListener('abort',abort,{once:true});
      img.src=tile.browserURL.replaceAll('{z}',tile.z).replaceAll('{x}',tile.x).replaceAll('{y}',tile.y);
    });
  }
paint(ctx,view,w,h,pass='all'){
  if(!this.enabled||!view)return;

  for(const tile of this.visible){

    // 第一遍只绘制普通底图
    if(pass==='base' && tile.isAnnotation){
      continue;
    }

    // 第二遍只绘制中文注记
    if(pass==='annotation' && !tile.isAnnotation){
      continue;
    }

    const bitmap=this.cache.get(tile.key);

    if(bitmap){
      ctx.drawImage(
        bitmap,
        w/2+(tile.left-view.x)/view.res,
        h/2-(tile.top-view.y)/view.res,
        tile.span/view.res+.3,
        tile.span/view.res+.3
      );
    }
  }
}
}
