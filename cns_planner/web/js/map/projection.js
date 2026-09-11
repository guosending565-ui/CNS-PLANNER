const R=6378137,MAX_LAT=85.05112878;
export function lonLatToMercator(lon,lat){const limited=Math.max(-MAX_LAT,Math.min(MAX_LAT,lat));return [lon*Math.PI/180*R,R*Math.log(Math.tan(Math.PI/4+limited*Math.PI/360))];}
export function mercatorToLonLat(x,y){return [x/R*180/Math.PI,(2*Math.atan(Math.exp(y/R))-Math.PI/2)*180/Math.PI];}
export function screenPoint(coordinate,view,width,height){const p=lonLatToMercator(...coordinate);return [width/2+(p[0]-view.x)/view.res,height/2-(p[1]-view.y)/view.res];}
export function eventLonLat(event,element,view,width,height){const rect=element.getBoundingClientRect();return mercatorToLonLat(view.x+(event.clientX-rect.left-width/2)*view.res,view.y-(event.clientY-rect.top-height/2)*view.res);}
