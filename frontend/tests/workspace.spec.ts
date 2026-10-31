import {test,expect,Page} from '@playwright/test';

const lessons=[{id:1,course_id:1,position:1,title:'Getting Started',passing_score:70},{id:2,course_id:1,position:2,title:'Practice',passing_score:70},{id:3,course_id:1,position:3,title:'Apply Your Skills',passing_score:70}];
const initialRows=lessons.map(l=>({student_id:1,lesson_id:l.id,completion_seen:0,quiz_passed:0,eligible:l.id===1?1:0,completed:0}));
const earnedRows=initialRows.map(r=>({...r,completion_seen:r.lesson_id===1?1:0,quiz_passed:r.lesson_id===1?1:0,completed:r.lesson_id===1?1:0,eligible:r.lesson_id<=2?1:0}));
const view={student_id:1,course_id:1,initialized:true,version:1,progress_fingerprint:'mock-fingerprint',refreshed_at:'2026-10-31 12:00:00',lessons:initialRows,course_completed:false};
const good={consistent:true,mismatches:[]};const bad={consistent:false,mismatches:[{lesson_id:2,field:'eligible',actual:0,expected:1}]};
const snapshot={progress:earnedRows,learner_view:view,verification:{consistent:false,progress:good,learner_view:bad,browser_ui_verified:false}};

async function mockBackend(page:Page, options:{uninitialized?:boolean;error?:boolean}={}){
  const state={posts:[] as {path:string;body:Record<string,unknown>}[],complete:false,started:false,authoritativeReads:0};
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname.replace('/api','');
    if(route.request().method()==='POST')state.posts.push({path,body:route.request().postDataJSON()});
    let data:unknown;
    if(path==='/students')data=[{id:1,name:'Synthetic Alex'},{id:2,name:'Synthetic Sam'}];
    else if(path==='/courses')data=[{id:1,title:'Learning Foundations'}];
    else if(path==='/lessons')data=lessons;
    else if(path==='/web/config')data={demo_mode:true,paid_investigations:true,execution_mode:'mocked_test'};
    else if(path==='/web/runs/active')data={run:null};
    else if(path==='/students/1/progress'){state.authoritativeReads++;data={lessons:earnedRows};}
    else if(path.endsWith('/learner-view')){
      if(options.error){await route.fulfill({status:503,json:{detail:'Mocked learner-view outage'}});return;}
      data=options.uninitialized?{...view,initialized:false,version:0,lessons:[],course_completed:null}:state.complete?{...view,version:2,lessons:earnedRows}:view;
    }else if(path==='/web/runs'||path==='/web/runs/mock-run'){
      state.started=true;
      const finished=state.complete;
      const events=[{event:'start',time:'2026-10-31T12:00:00Z'},{event:'tool_result',tool:'inspect_view',evidence_ref:'tool-0001',time:'2026-10-31T12:00:01Z',result:{ok:true,data:view}}];
      if(finished)events.push({event:'tool_result',tool:'refresh_view',evidence_ref:'tool-0002',time:'2026-10-31T12:00:02Z',result:{ok:true,data:view}});
      data={id:'mock-run',student_id:1,course_id:1,status:finished?'finished':'running',started_at:'2026-10-31T12:00:00Z',completed_at:finished?'2026-10-31T12:00:03Z':null,
        execution_mode:'mocked_test',model_name:'Mocked browser-test model',events,before:snapshot,
        after:finished?{progress:earnedRows,learner_view:{...view,version:2,lessons:earnedRows},verification:{consistent:true,progress:good,learner_view:good,browser_ui_verified:false}}:null,
        report:finished?{status:'recovered',diagnosis:'Mocked test result: the saved view was refreshed.',remaining_limitations:['Mocked browser test, not a live AI result.'],recommended_next_steps:[]}:null,error_code:null};
    }else {await route.fulfill({status:404,json:{detail:'Unmocked API path rejected'}});return;}
    await route.fulfill({json:data});
  });return state;
}

test('dashboard displays stale persisted view, never authoritative progress',async({page})=>{
  const state=await mockBackend(page);await page.goto('/');
  await expect(page.getByRole('heading',{name:'Your lessons'})).toBeVisible();
  await expect(page.locator('.progress-ring strong')).toHaveText('0%');
  await expect(page.locator('.lesson-card').nth(1).getByText('Locked',{exact:true})).toBeVisible();
  await expect(page.getByText('Mocked agent responses · test only. This is not a live AI result.')).toBeVisible();
  expect(state.authoritativeReads).toBe(0);expect(state.posts).toHaveLength(0);
  await page.screenshot({path:'test-results/frontend-dashboard.png',fullPage:true});
});

test('explicit consent starts one mocked run, polls real-shaped events, then refetches cache',async({page})=>{
  const state=await mockBackend(page);await page.goto('/');
  await page.getByRole('button',{name:'Recovery console'}).click();
  const investigate=page.getByRole('button',{name:'Investigate with AI'});
  await expect(investigate).toBeDisabled();expect(state.posts).toHaveLength(0);
  await page.getByRole('checkbox').check();await investigate.click();
  await expect(page.getByRole('button',{name:'Investigation active'})).toBeDisabled();
  await expect(page.getByRole('button',{name:'Load scenario'})).toBeDisabled();
  await expect(page.getByLabel('Learner',{exact:true})).toBeDisabled();
  await expect(page.locator('.timeline .event-label').filter({hasText:'inspect_view'})).toBeVisible();
  expect(state.posts.filter(p=>p.path==='/web/runs')).toHaveLength(1);
  expect(state.posts[0].body.allow_paid).toBe(true);
  expect(state.posts[0].body.symptom).not.toContain('combined');
  state.complete=true;
  await expect(page.getByText('Mocked test result: the saved view was refreshed.')).toBeVisible();
  await expect(page.getByRole('heading',{name:'Before & after'})).toBeVisible();
  await expect(page.getByText('Browser/UI verification has not been performed by the agent.',{exact:false})).toBeVisible();
  await page.getByRole('button',{name:'Student dashboard'}).click();
  await expect(page.locator('.progress-ring strong')).toHaveText('33%');
  await expect(page.locator('.lesson-card').nth(1).getByText('Available',{exact:true})).toBeVisible();
  expect(state.posts.filter(p=>p.path==='/web/runs')).toHaveLength(1);
  expect(state.posts.filter(p=>p.path.includes('refresh'))).toHaveLength(0);
  expect(state.authoritativeReads).toBe(0);
});

test('uninitialized view stays explicit and page load is mutation-free',async({page})=>{
  const state=await mockBackend(page,{uninitialized:true});await page.goto('/');
  await expect(page.getByText('Your learner view hasn’t been published.')).toBeVisible();
  await expect(page.locator('.progress-ring strong')).toHaveText('—');expect(state.posts).toHaveLength(0);
});

test('read errors and mobile layout do not launch investigations',async({page})=>{
  const state=await mockBackend(page,{error:true});await page.setViewportSize({width:390,height:844});await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('Mocked learner-view outage');
  await page.getByRole('button',{name:'Try reading again'}).click();
  expect(state.posts).toHaveLength(0);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
});
