export type Student = {id:number; name:string};
export type Course = {id:number; title:string};
export type Lesson = {id:number; course_id:number; position:number; title:string; passing_score:number};
export type Progress = {student_id:number; lesson_id:number; completion_seen:number; quiz_passed:number; eligible:number; completed:number};
export type LearnerView = {student_id:number; course_id:number; initialized:boolean; version:number; progress_fingerprint:string|null; refreshed_at:string|null; lessons:Progress[]; course_completed:boolean|null};
export type Check = {consistent:boolean; mismatches:unknown[]};
export type Verification = {consistent:boolean; progress:Check; learner_view:Check; browser_ui_verified:false};
export type Snapshot = {progress:Progress[]; learner_view:LearnerView; verification:Verification};
export type Trace = {time:string; event:string; tool?:string; evidence_ref?:string; result?:{ok:boolean; error_code?:string; data?:unknown}; usage?:{input_tokens:number; output_tokens:number; total_tokens:number}; [key:string]:unknown};
export type Report = {status:string; diagnosis:string; remaining_limitations:string[]; recommended_next_steps:string[]; verification_outcome:Verification};
export type Run = {id:string; student_id:number; course_id:number; status:'running'|'finished'|'execution_failed'; started_at:string; completed_at:string|null; execution_mode:'mocked_test'|'live_openai'; model_name:string; events:Trace[]; before:Snapshot; after:Snapshot|null; report:Report|null; error_code:string|null};
export type Config = {demo_mode:boolean; paid_investigations:boolean; execution_mode:string};
export async function api<T>(path:string, body?:unknown, signal?:AbortSignal):Promise<T> {
  const response = await fetch(`/api${path}`, {method:body===undefined?'GET':'POST', headers:body===undefined?undefined:{'Content-Type':'application/json'}, body:body===undefined?undefined:JSON.stringify(body), signal});
  if(!response.ok) {const error=await response.json().catch(()=>null); throw new Error(typeof error?.detail==='string'?error.detail:`Request failed (${response.status})`);}
  return response.json();
}
export function status(row?:Progress) {return !row?'Not published':row.completed?'Completed':row.eligible?'Available':'Locked';}
export function timestamp(value:string|null) {if(!value)return 'Not published'; const parsed = new Date(value.includes('T')?value:value.replace(' ','T')+'Z'); return parsed.toLocaleString();}
