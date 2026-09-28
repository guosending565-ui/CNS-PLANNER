/**
 * =========================================================================
 * 壳层动作：报告与项目保存（从 `main.js` 抽出，保持入口精简）
 * =========================================================================
 *
 * `main.js` 有既有的架构约束（轻入口），而报告预览 / 报告下载 / 项目保存都是
 * **纯 UI 动作编排**：它们只调用既有 API、只更新既有面板提示，不含任何业务判定。
 * 因此集中在本模块，`main.js` 只注入依赖。
 *
 * 语义保持（与抽出前逐字一致）
 * --------------------------
 *  - 报告预览走 `POST /api/cns-planning-report/preview`，只在新窗口显示，**不写入项目**；
 *  - 报告下载只接受**已有正式报告**的 `active_report_id`，没有报告时给出明确中文原因，
 *    绝不自动生成、也不静默失败；
 *  - 保存项目只走 `POST /api/workflow/project` + `POST /api/project/save-as`，
 *    没有选择存储位置时如实提示，不猜路径。
 */
import {sourceStateText} from './presentation.js';
import {recordExplicitProject} from '../state/explicit_project.js';

/**
 * @param {object} deps
 * @param {(id:string)=>any} deps.getNode DOM 查找（`$`）
 * @param {(message:string)=>void} deps.panelError 面板错误行
 */
export function createShellActions({getNode,panelError}){
  for(const [name,value] of Object.entries({getNode,panelError})){
    if(typeof value!=='function')throw new Error('createShellActions 缺少依赖：'+name);
  }

  /**
   * 预览报告草稿（新窗口）。预览**不写入项目**。
   * @param {(path:string,payload:object)=>Promise<object>} computeAction
   */
  async function previewReport(computeAction){
    const target=window.open('about:blank','_blank');
    try{
      const result=await computeAction('/api/cns-planning-report/preview',{});
      const blob=new Blob([result.html],{type:'text/html;charset=utf-8'}),url=URL.createObjectURL(blob);
      if(target)target.location.href=url;
      else throw Error('浏览器阻止了预览窗口，请允许本地工作台打开新窗口');
      setTimeout(()=>URL.revokeObjectURL(url),60000);
      panelError('报告草稿已在新窗口打开；预览不会写入项目。');
    }catch(exc){
      if(target)target.close();
      throw Error('报告预览失败：'+exc.message+'。请检查项目状态后重试。');
    }
  }

  /**
   * 下载报告 / 规划数据包。
   *
   * @param {object} input
   * @param {(url:string)=>Promise<Blob>} input.api
   * @param {object} input.flow 当前 workflow（只读 `cns_planning_reports`）
   * @param {(reason:string)=>void} input.onMissing 没有正式报告时的处理（面板提示）
   */
  async function downloadReport(kind,{api,flow,onMissing}={}){
    const reports=(flow&&flow.cns_planning_reports)||{},reportId=reports.active_report_id;
    if(!reportId){
      const message='尚无正式报告。请先在方案评审中选择方案、确认并应用，再点击“生成正式报告”。';
      if(typeof onMissing==='function')onMissing(message);
      throw Error(message);
    }
    const url='/api/cns-planning-report/artifact?'+new URLSearchParams({report_id:reportId,kind});
    const blob=await api(url),objectUrl=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=objectUrl;
    link.download={html:'cns-planning-report.html',pdf:'cns-planning-report.pdf',
      package:'cns-planning-package.zip',json:'cns-planning-report.json'}[kind]||'report.bin';
    link.click();
    setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);
  }

  /**
   * 顶部全局“保存项目”（Step01 的保存按钮是同一个入口）。
   *
   * BUG-UI-SAVE-001：保存协议**完全不变**（POST /api/workflow/project → POST /api/project/save-as
   * → GET /api/state），修复的是"点击后毫无反馈"：
   *  * 点击后按钮立即进入「正在保存…」并禁用，避免重复提交；
   *  * 成功给出中文结论 + **当前项目目录**（来自保存后 fresh state 的 project_storage）；
   *  * 失败给出中文原因，并把技术细节（error.name / HTTP 状态）一并回传，
   *    由壳层放进「高级 / 审计信息」式的错误行，而不是只说一句"失败"；
   *  * 无论成功失败都恢复按钮（面板被重渲染时按需重挂）。
   *
   * @param {object} input
   * @param {(url:string,options:object)=>Promise<object>} input.api
   * @param {object} input.state 服务器状态（只读 `project_storage`）
   * @param {object} input.flow 当前 workflow
   * @param {(projectDir:string,name:string)=>Promise<void>} input.saveAs
   * @param {(flow:object)=>Promise<void>|void} input.applyFlow 把新的 workflow 写回壳层
   * @param {(data:object)=>Promise<void>|void} input.applyState 保存成功后刷新服务器状态
   * @returns {Promise<{ok:boolean, message:string, directory:string, error:Error|null}>}
   */
  async function saveProject({projectDir,name}={},{
    api,state,flow,saveAs,applyFlow,applyState
  }={}){
    const requestedDir=String(projectDir||'').trim();
    const directory=requestedDir||state?.project_storage?.directory||'';
    // 面板提示行同时承载"错误"与"确认"，因此按语义着色（第二个参数是可选的 tone）。
    const say=(message,tone)=>panelError(message,tone);
    if(!directory){
      const message='请先在第 01 步选择项目数据存储位置（当前尚未选择项目目录）。';
      say(message,'error');
      return {ok:false,message,directory:'',error:null};
    }
    const button=getNode('saveProjectTop')||getNode('saveProject');
    const original=button?button.textContent:null;
    try{
      if(button){button.disabled=true;button.textContent='正在保存…';}
      say('正在保存项目…','hint');
      const projectName=name||getNode('projectName')?.value||flow?.project?.name||'';
      const data=await api('/api/workflow/project',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:projectName})
      });
      if(typeof applyFlow==='function')await applyFlow(data);
      await saveAs(directory);
      const freshState=await api('/api/state');
      recordExplicitProject(freshState);
      if(typeof applyState==='function')await applyState(freshState);
      const savedDir=String(freshState?.project_storage?.directory||directory);
      const message='项目已保存'+(freshState?.project_storage?.automatic===true?'（仍为自动恢复项目）':'')
        +' · 项目目录：'+savedDir;
      say(message,'success');
      return {ok:true,message,directory:savedDir,error:null};
    }catch(exc){
      // 技术细节（error.name / HTTP 状态码）如实保留，供"高级 / 审计信息"使用。
      const detail=[exc?.name,exc?.status?('HTTP '+exc.status):''].filter(Boolean).join(' · ');
      const message='保存项目失败：'+(exc?.message||exc)+(detail?'（技术细节：'+detail+'）':'')
        +' · 项目目录：'+directory;
      say(message,'error');
      return {ok:false,message,directory,error:exc||new Error(String(exc))};
    }finally{
      if(button&&document.body.contains(button)){
        button.disabled=false;
        if(original!==null)button.textContent=original;
      }
    }
  }

  /** 没有正式报告时的中文原因（供状态栏 / 空状态复用）。 */
  function reportMissingReason(){return sourceStateText('no_result');}

  return {previewReport,downloadReport,saveProject,reportMissingReason};
}
