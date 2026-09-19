import {render} from '../cns_planner/web/js/workflow/step06_review.js';
const html=render({state:{data_health:{status:'passed'}},flow:{
  project:{name:'P'},workspace:null,operational_routes:[],aircraft:null,rules:null,coverage:null,
  review:{risks:{},overall_status:'pending_confirmation',overall_pass:false},
  result_statuses:{cns_corridor_site_plan:'passed'},
  cns_corridor_site_plan:{status:'proposal_ready',target_voxel_count:2,selected_actions:[{}],confirmed_requirement_unit_volume_gain:10}
}});
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
console.log('ids',ids.length,'unique',new Set(ids).size);
console.log('dupes',ids.filter((id,index)=>ids.indexOf(id)!==index));
console.log('segments',[...html.matchAll(/data-seg-name="([a-z0-9-]+)" data-seg-label="([^"]*)"/g)].map(m=>m[1]+':'+m[2]));
console.log('comments',(html.match(/<!--/g)||[]).length);
console.log('segset',(html.match(/data-seg-set="(?!none)/g)||[]).length);
console.log('seglabels',(html.match(/data-seg-label="/g)||[]).length);
console.log('hasPlanReviewTitle',html.includes('方案审查（Plan Review）与受控应用'));
console.log('applyPlanDisabled',/id="applyPlan"[^>]*disabled/.test(html));
console.log('generateDisabled',/id="generatePlanningReport"[^>]*disabled/.test(html));
console.log('html len',html.length);
