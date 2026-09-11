export function bindMapInteraction(options){
  const {map,canvas,getView,setView,getMode,eventLonLat,zoom,queue,paint,onDraft,onDraftComplete,onNode,onPosition,onPanStart}=options;
  let drag=null,drawStart=null;
  map.addEventListener('wheel',event=>{
    if(event.target!==canvas)return;event.preventDefault();
    const rect=map.getBoundingClientRect();zoom(event.deltaY>0?1.25:.8,event.clientX-rect.left,event.clientY-rect.top);
  },{passive:false});
  canvas.addEventListener('pointerdown',event=>{
    const view=getView();if(!view)return;
    if(getMode()==='workspace'){
      drawStart=eventLonLat(event);onDraft([drawStart[0],drawStart[1],drawStart[0],drawStart[1]]);
      canvas.setPointerCapture(event.pointerId);return;
    }
    if(getMode()==='node')return;
    drag={x:event.clientX,y:event.clientY,cx:view.x,cy:view.y};canvas.setPointerCapture(event.pointerId);map.classList.add('dragging');onPanStart();
  });
  canvas.addEventListener('pointermove',event=>{
    const view=getView();if(!view)return;
    if(drawStart){const now=eventLonLat(event);onDraft([Math.min(drawStart[0],now[0]),Math.min(drawStart[1],now[1]),Math.max(drawStart[0],now[0]),Math.max(drawStart[1],now[1])]);paint();return;}
    if(drag){view.x=drag.cx-(event.clientX-drag.x)*view.res;view.y=drag.cy+(event.clientY-drag.y)*view.res;setView(view);paint();}
    onPosition(eventLonLat(event));
  });
  canvas.addEventListener('pointerup',async event=>{
    if(drawStart){drawStart=null;paint();onDraftComplete();return;}
    if(getMode()==='node'){await onNode(eventLonLat(event));return;}
    if(drag){drag=null;map.classList.remove('dragging');queue();}
  });
  canvas.addEventListener('pointercancel',()=>{drawStart=null;drag=null;map.classList.remove('dragging');});
  canvas.addEventListener('dblclick',event=>{if(getMode()!=='pan')return;const rect=map.getBoundingClientRect();zoom(.5,event.clientX-rect.left,event.clientY-rect.top);});
}
