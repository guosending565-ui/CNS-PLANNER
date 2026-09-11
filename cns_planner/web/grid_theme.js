(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.GridTheme=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  const NO_DATA_COLOR='#aeb7c2';

  function quantileBreaks(values,classCount=5){
    const clean=values.filter(value=>value!==null&&value!==''&&value!==undefined).map(Number).filter(Number.isFinite).sort((a,b)=>a-b);
    if(!clean.length)return [];
    const breaks=[];
    for(let index=0;index<=classCount;index++){
      const position=(clean.length-1)*index/classCount;
      const lower=Math.floor(position),upper=Math.ceil(position),ratio=position-lower;
      breaks.push(clean[lower]+(clean[upper]-clean[lower])*ratio);
    }
    return breaks;
  }

  function colorForValue(value,breaks,palette){
    if(value===null||value===''||value===undefined)return NO_DATA_COLOR;
    const number=Number(value);
    if(!Number.isFinite(number)||!breaks.length)return NO_DATA_COLOR;
    for(let index=1;index<breaks.length;index++){
      if(number<=breaks[index])return palette[Math.min(index-1,palette.length-1)];
    }
    return palette[palette.length-1];
  }

  function formatNumber(value){
    const number=Number(value);
    if(!Number.isFinite(number))return '无数据';
    return Math.abs(number)>=1000
      ? number.toLocaleString('zh-CN',{maximumFractionDigits:1})
      : number.toLocaleString('zh-CN',{maximumFractionDigits:2});
  }

  function bboxIntersects(left,right){
    return left[0]<right[2]&&left[2]>right[0]&&left[1]<right[3]&&left[3]>right[1];
  }

  function bboxContainsHalfOpen(bbox,lon,lat){
    return bbox[0]<=lon&&lon<bbox[2]&&bbox[1]<=lat&&lat<bbox[3];
  }

  return {NO_DATA_COLOR,quantileBreaks,colorForValue,formatNumber,bboxIntersects,bboxContainsHalfOpen};
});
