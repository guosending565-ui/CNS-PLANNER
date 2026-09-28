/**
 * =========================================================================
 * 同步耗时请求的统一反馈（BUG-TASK-FEEDBACK-001 §6）
 * =========================================================================
 *
 * 正式业务计算里仍有一部分是**同步 POST**（例如「运行 Theta* V2 candidate」）。
 * 它们不适合在本轮改造成后台任务，但绝不允许"点了以后毫无反馈"：
 *
 *   * 按钮立即进入「正在提交…」，并禁用，避免用户重复提交；
 *   * 拿到响应之前显示「正在计算，请勿重复提交」与 **已运行时长**；
 *   * 请求结束后恢复按钮原文案与可用状态；
 *   * 失败时把**中文业务原因**写到按钮附近的提示行（`panelError`），
 *     `no_path` 这类"搜索已完成但没有路径"的结论也必须说清楚，不能让用户以为算法卡死。
 *
 * 这里**不重写**任务框架：它只负责同步路径的按钮态与计时，后台任务路径仍然走
 * `tasks.js` 的 `/api/tasks`（排队 / 进度 / 心跳 / 取消）。
 */

/** 已运行时长中文：`12 秒` / `2 分 10 秒`。与 tasks.js 的 durationText 同源语义。 */
export function elapsedText(seconds){
  const value=Number(seconds);
  if(!Number.isFinite(value)||value<0)return '—';
  if(value<60)return Math.round(value)+' 秒';
  const minutes=Math.floor(value/60);
  const rest=Math.round(value-minutes*60);
  return rest?minutes+' 分 '+rest+' 秒':minutes+' 分';
}

export const BUSY_SUBMIT_TEXT='正在提交…';
export const BUSY_RUNNING_TEXT='正在计算，请勿重复提交';

/**
 * 把一次同步耗时操作包装成"有自述状态的按钮"。
 *
 * @param {object} input
 * @param {{disabled:boolean, textContent:string, title?:string}} input.button 触发按钮
 * @param {() => Promise<any>} input.run 真正的请求
 * @param {(message:string)=>void} [input.onError] 中文业务错误出口
 * @param {(data:any)=>void} [input.onDone] 成功后的业务收尾（重渲染等）
 * @param {string} [input.label] 业务任务名（用于提示文案，例如「航路规划」）
 * @param {number} [input.elapsedAfterMs] 多久之后把已用时长显示到 title（默认 1000ms）
 * @param {() => number} [input.now] 时钟注入（测试用）
 * @returns {Promise<{ok:boolean,data:any,error:Error|null,elapsed_ms:number}>}
 */
export async function runWithBusyButton({
  button, run, onError, onDone, label='', elapsedAfterMs=1000, now=()=>Date.now(),
}){
  if(typeof run!=='function')throw new Error('runWithBusyButton 缺少 run');
  const originalText=button?button.textContent:null;
  const originalTitle=button?button.title:null;
  const started=now();
  let timer=null;
  const tick=()=>{
    if(!button)return;
    const seconds=(now()-started)/1000;
    button.title=(label?label+'：':'')+BUSY_RUNNING_TEXT+'（已运行 '+elapsedText(seconds)+'）';
  };
  try{
    if(button){button.disabled=true;button.textContent=BUSY_SUBMIT_TEXT;}
    if(button&&Number.isFinite(Number(elapsedAfterMs))){
      timer=setInterval(tick,Math.max(500,Number(elapsedAfterMs)));
    }
    const data=await run();
    if(typeof onDone==='function')await onDone(data);
    return {ok:true,data,error:null,elapsed_ms:now()-started};
  }catch(error){
    const failure=error||new Error(String(error));
    if(typeof onError==='function'){
      onError(failure?.message||String(failure));
      // 已经给出中文业务原因：返回结果而不是重新抛出，避免外层 `actionButton` 再报一次同样的错。
      return {ok:false,data:null,error:failure,elapsed_ms:now()-started,handled:true};
    }
    throw failure;
  }finally{
    if(timer!==null)clearInterval(timer);
    if(button){
      // 面板可能已经被重渲染替换掉了这个节点：只在原节点仍挂载时恢复。
      const alive=typeof document==='undefined'||!document.body||document.body.contains(button);
      if(alive){
        button.disabled=false;
        if(originalText!==null)button.textContent=originalText;
        if(originalTitle!==null)button.title=originalTitle;
      }
    }
  }
}
