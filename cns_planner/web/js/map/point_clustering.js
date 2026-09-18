// =========================================================
// 屏幕空间点聚合
//
// 只为显示服务：不修改 flow、不修改任何数据，也不改变真实 geometry。
// 待聚合的坐标先投影到屏幕像素，再在像素空间做贪心网格聚类，
// 因此"远近"始终以当前视图的真实视觉距离为准。
//
// 交互约定：
//  - 聚合点可点击放大到其覆盖范围（由调用方传入放大能力）；
//  - 若调用方没有提供放大能力，overview/medium 下被聚合的单个点不参与
//    命中测试，避免点到看不见的单个点。
// =========================================================

function bucketKey(x,y){
  return Math.round(x)+':'+Math.round(y);
}

function renderable(coordinate){
  return Array.isArray(coordinate)&&Number.isFinite(coordinate[0])&&Number.isFinite(coordinate[1]);
}

/**
 * 把点集按屏幕距离聚合。
 * @param {Array<{id?:string,coordinate:number[]}>} items
 * @param {Function} toScreen coordinate => [x,y]
 * @param {number} thresholdPx 屏幕聚合半径（像素）；<=0 或非有限值表示不聚合
 * @returns {{entries:Array,byId:Map<string,object>}}
 *   entry = {id,count,coordinate,screen,radiusPx,members,anchor,single}
 */
export function clusterPoints(items,toScreen,thresholdPx){
  const list=(items||[]).filter(item=>item&&renderable(item.coordinate));
  const noClustering=!(Number.isFinite(thresholdPx)&&thresholdPx>0);

  if(noClustering){
    const entries=list.map((item,index)=>{
      const screen=toScreen(item.coordinate);
      const id=item.id!==undefined&&item.id!==null?String(item.id):'single-'+index;
      return {id,count:1,coordinate:item.coordinate,screen:[Number(screen[0]),Number(screen[1])],radiusPx:0,members:[item],anchor:item,single:true};
    });
    return {entries,byId:new Map(entries.map(entry=>[entry.id,entry]))};
  }

  const projected=list.map((item,index)=>{
    const screen=toScreen(item.coordinate);
    return {item,index,screen:[Number(screen[0]),Number(screen[1])]};
  }).filter(entry=>Number.isFinite(entry.screen[0])&&Number.isFinite(entry.screen[1]));

  projected.sort((a,b)=>a.screen[0]-b.screen[0]);

  const cellSize=thresholdPx;
  const grid=new Map();
  const clusters=[];

  for(const entry of projected){
    const cellX=Math.floor(entry.screen[0]/cellSize),cellY=Math.floor(entry.screen[1]/cellSize);
    let target=-1;
    for(let dx=-1;dx<=1&&target<0;dx++){
      for(let dy=-1;dy<=1&&target<0;dy++){
        const bucket=grid.get(bucketKey(cellX+dx,cellY+dy));
        if(!bucket)continue;
        for(const index of bucket){
          const candidate=clusters[index];
          if(Math.hypot(candidate.center[0]-entry.screen[0],candidate.center[1]-entry.screen[1])<=thresholdPx){target=index;break;}
        }
      }
    }
    if(target<0){
      const index=clusters.length;
      clusters.push({center:entry.screen.slice(),sum:entry.screen.slice(),count:1,members:[entry.item],screens:[entry.screen]});
      const key=bucketKey(cellX,cellY);
      const bucket=grid.get(key);
      if(bucket)bucket.push(index);else grid.set(key,[index]);
    }else{
      const candidate=clusters[target];
      candidate.sum[0]+=entry.screen[0];
      candidate.sum[1]+=entry.screen[1];
      candidate.count+=1;
      candidate.members.push(entry.item);
      candidate.screens.push(entry.screen);
      candidate.center=[candidate.sum[0]/candidate.count,candidate.sum[1]/candidate.count];
    }
  }

  const entries=clusters.map((cluster,index)=>{
    const single=cluster.count===1;
    let radiusPx=0;
    if(!single){
      // 覆盖半径取成员到显示中心的最大屏幕距离，保证命中范围覆盖全部成员
      radiusPx=cluster.screens.reduce((max,screen)=>Math.max(max,Math.hypot(screen[0]-cluster.center[0],screen[1]-cluster.center[1])),0);
    }
    return {
      id:single?'single-'+index:'cluster-'+index,
      count:cluster.count,
      // 单点始终保留原始坐标（不重新投影，避免任何坐标漂移）；
      // 聚合点先给出屏幕中心，由 toGeographic 换算成地理显示坐标。
      coordinate:single?cluster.members[0].coordinate:null,
      center:cluster.center,           // 屏幕坐标（聚合显示位置）
      screen:single?cluster.screens[0]:cluster.center,
      radiusPx:single?0:Math.max(radiusPx,10),
      members:cluster.members,
      screens:cluster.screens,
      anchor:cluster.members[0],
      single
    };
  });

  return {entries,byId:new Map(entries.map(entry=>[entry.id,entry]))};
}

/**
 * 把屏幕空间的聚合结果换算回地理坐标。
 * 单个点沿用原始坐标（不重新投影，避免引入任何坐标漂移）。
 * @param {Array} entries clusterPoints 的结果
 * @param {Function} fromScreen [x,y] => coordinate
 */
export function toGeographic(entries,fromScreen){
  return (entries||[]).map(entry=>{
    if(entry.single||typeof fromScreen!=='function')return entry;
    return {...entry,coordinate:fromScreen(entry.center)};
  });
}

/**
 * 命中测试：找到屏幕点命中的聚合/单点条目。
 * 聚合条目按覆盖半径命中；单点按固定像素半径命中。
 */
export function hitCluster(entries,point,{singleRadius=9}={}){
  const [x,y]=point;
  let best=null,bestDistance=Infinity;
  for(const entry of entries||[]){
    const screen=entry.screen||entry.center;
    if(!screen)continue;
    const distance=Math.hypot(screen[0]-x,screen[1]-y);
    const limit=entry.single?singleRadius:Math.max(entry.radiusPx||0,10)+3;
    if(distance<=limit&&distance<bestDistance){best=entry;bestDistance=distance;}
  }
  return best;
}

/** 当前聚合条目的屏幕 2D 包围盒（供"放大到该范围"使用）。 */
export function extentOf(entry){
  if(!entry||entry.single)return null;
  const screens=(entry.screens&&entry.screens.length?entry.screens:[]).filter(Array.isArray);
  if(!screens.length)return null;
  const xs=screens.map(screen=>screen[0]),ys=screens.map(screen=>screen[1]);
  return [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];
}

/** 聚合点显示文本：单个返回自身标签，多个返回数量。 */
export function clusterLabel(entry,{singleLabel='',countSuffix='处'}={}){
  if(!entry)return '';
  return entry.count===1?(singleLabel||''):entry.count+countSuffix;
}

/** 显示坐标：单点用原始坐标，聚合点用换算后的地理中心。 */
export function displayCoordinate(entry){
  if(!entry)return null;
  return entry.single?entry.anchor.coordinate:entry.coordinate;
}
