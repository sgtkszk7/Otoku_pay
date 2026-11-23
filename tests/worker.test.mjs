import {test} from "node:test";
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import fs from "node:fs";

const moduleSource = fs.readFileSync(new URL("../worker/worker.js",import.meta.url));
const {default:worker,LookupCoordinator} = await import("data:text/javascript;base64,"+moduleSource.toString("base64"));
const code = "A".repeat(40);
const digest = createHash("sha256").update(code).digest("hex");
function fixture(){
  const map = new Map();
  let tail = Promise.resolve();
  const storage = {
    async get(k){return Array.isArray(k)?new Map(k.map(id=>[id,map.get(id)])):map.get(k);},
    async put(k,v){if(typeof k==="string") map.set(k,v); else for(const pair of Object.entries(k)) map.set(...pair);},
    transaction(fn){const p=tail.then(()=>fn(storage));tail=p.catch(()=>{});return p;},
    getAlarm:async()=>1
  };
  const env={LOOKUP_USERS:JSON.stringify({[digest]:"reader"}),ANTHROPIC_API_KEY:"fixture",
    USER_DAILY_CALLS:5,MONTHLY_CALLS:10,CALL_RESERVE_CENTS:3,MONTHLY_BUDGET_CENTS:30};
  const make = ()=>new LookupCoordinator({storage},env);
  let instance=make();
  env.LOOKUP={idFromName:x=>x,get:()=>({fetch:(url,init)=>instance.fetch(new Request(url,init))})};
  const send=(body,token=code)=>worker.fetch(new Request("https://worker.example",{
    method:"POST",headers:{Origin:"https://sgtkszk7.github.io",Authorization:"Bearer "+token},
    body:JSON.stringify(body)}),env);
  return {env,send,map,restart:()=>{instance=make();}};
}
function upstream(t,handler){
  let calls=0;
  t.mock.method(globalThis,"fetch",async url=>{
    if(url.endsWith("/data.json")) return Response.json({methods:[]});
    assert.equal(url,"https://api.anthropic.com/v1/messages");
    return Response.json(await handler(++calls));
  });
  return ()=>calls;
}
const missing=()=>({content:[{type:"text",text:JSON.stringify({found:false})}]});
test("paid lookup needs valid credentials, configuration and bounded typed input",async t=>{
  const f=fixture(), calls=upstream(t,missing);
  assert.equal((await f.send({store:"テスト"},"")).status,401);
  for (const input of [null,[],{store:42},{store:"長".repeat(3000)}]){
    assert.equal((await f.send(input)).status,400);
  }
  f.env.MONTHLY_BUDGET_CENTS=0;
  assert.equal((await f.send({store:"テスト"})).status,503);
  assert.equal(calls(),0);
});
test("concurrent normalized queries share one charge and negative results survive restart",async t=>{
  const f=fixture(), calls=upstream(t,missing);
  const responses=await Promise.all([f.send({store:" ＡＢＣ "}),f.send({store:"abc"})]);
  assert.deepEqual(responses.map(r=>r.status),[200,200]); assert.equal(calls(),1);
  f.restart(); await f.send({store:"ABC"}); assert.equal(calls(),1);
  assert.equal(f.map.get("monthly").reservedCents,3);
});
test("every continuation reserves global budget before contacting the provider",async t=>{
  const f=fixture();f.env.MONTHLY_BUDGET_CENTS=6;
  const calls=upstream(t,()=>({stop_reason:"pause_turn",content:[]}));
  assert.equal((await f.send({store:"調査"})).status,429);
  assert.equal(calls(),2);
  f.restart(); assert.equal((await f.send({store:"別の店"})).status,429);
  assert.equal(calls(),2);
});
test("independent queries cannot overdraw the same user allowance",async t=>{
  const f=fixture(); f.env.USER_DAILY_CALLS=1;
  const calls=upstream(t,missing);
  const responses=await Promise.all([f.send({store:"甲"}),f.send({store:"乙"})]);
  assert.deepEqual(responses.map(r=>r.status).sort(),[200,429]);assert.equal(calls(),1);
});
