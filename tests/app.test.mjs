import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const read = name => fs.readFileSync(new URL("../"+name,import.meta.url),"utf8");
const data = JSON.parse(read("data.json"));
const script = read("index.html").match(/<script>([\s\S]+?)<\/script>/)[1];
function app(saved = {}) {
  const elements = new Map();
  const element = () => ({
    value:"", innerHTML:"", classList:{add(){},remove(){}}, addEventListener(){}, prepend(){},
    get outerHTML(){return '<a href="'+this.href+'">'+this.textContent+'</a>';}
  });
  const document = {
    querySelector(id){if(!elements.has(id)) elements.set(id,element()); return elements.get(id);},
    createElement:element, querySelectorAll:()=>[], addEventListener(){}
  };
  const ctx=vm.createContext({URL, document, data:structuredClone(data), location:{origin:"https://example.org"},
    localStorage:{getItem:()=>JSON.stringify(saved),setItem(){}},
    fetch:()=>new Promise(()=>{})});
  const run=code=>vm.runInContext(code,ctx);
  run(read("allocation.js")); run(script);
  run('loadData(data); restore(); const shop=id=>STORES.find(s=>s.id===id); const rows=id=>rank(shop(id));');
  return run;
}
test("store and review follow the displayed default and explicit point priority",()=>{
  const run=app({methods:["rakuten_card","paypay_card"],favs:["starbucks"]});
  assert.equal(run('rows("starbucks")[0].m.id'),"rakuten_card");
  assert.equal(run('suggest().unused.some(m=>m.id===rows("starbucks")[0].m.id)'),false);
  assert.equal(run('state.pointPrio=["PayPayポイント"]; rows("starbucks")[0].m.id'),"paypay_card");
  assert.equal(run('suggest().unused.some(m=>m.id==="paypay_card")'),false);
});
const STAR=500/(130*60)*100;  // スタバのStarを500円券で換算した％
test("charge reward requires a known pair and candidate wallet recalculates existing cards",()=>{
  const run=app({methods:["sbux_card","saison_pearl"],favs:["starbucks"],spend:{starbucks:10000}});
  assert.equal(run('rows("starbucks").find(r=>r.m.charge).rate'),STAR);
  assert.equal(run('amountLabel(rows("starbucks").find(r=>r.m.charge),1000)'),"Star 16個");
  assert.equal(run('state.methods=["sbux_card"]; METHODS.find(m=>m.id==="sbux_card").charge_rates={jcb_w:5.5}; suggest().out.find(o=>o.m.id==="jcb_w").yearly'),6600);
  assert.equal(run('state.charge.sbux_card="cash"; suggest().out.some(o=>o.yearly===6600)'),false);
});
test("month cap across stores, excess base rate, annual cap and alternative payment",()=>{
  const run=app({methods:["aeon_pay"],favs:["seven","lawson"],spend:{seven:20000,lawson:20000}});
  assert.equal(run("suggest().curMonthly"),400);
  assert.equal(run('state.methods.push("rakuten_card"); suggest().curMonthly'),500);
  assert.equal(run('state.methods=["aeon_pay"]; state.favs=["aeon"]; state.spend.aeon=40000; suggest().curMonthly'),500);
  assert.equal(run('state.methods=["saison_pearl"]; state.favs=["seven"]; state.spend.seven=40000; suggest().cur.total'),6900);
});
test("unknown and relative store points never produce absolute points or estimated yen",()=>{
  const run=app({methods:["paypay","rakuten_card"],favs:["joshin"]});
  assert.equal(run('rows("joshin")[0].m.id'),"cash");
  assert.equal(run('rows("joshin").find(r=>r.m.id==="paypay").kind'),"unknown");
  assert.equal(run('amountLabel(rows("joshin").find(r=>r.m.id==="rakuten_card"),1000)'),"");
  assert.equal(run("suggest().cur.entries.length"),0);
});
test("old method is migrated only for known variants and overrides do not leak",()=>{
  const run=app({methods:["smbc_nl"],usual:{seven:"smbc_nl:card_touch"},overrides:{smbc_nl:8}});
  assert.equal(run('usualOf(shop("seven"),rows("seven")).rate'),0.5);
  assert.equal(run('state.usual.seven="smbc_nl"; usualOf(shop("seven"),rows("seven"))'),null);
  assert.equal(run('state.usual.seven="smbc_nl:obsolete"; usualOf(shop("seven"),rows("seven"))'),null);
});
test("stored fields recover independently, source scheme checked on display",()=>{
  const run=app({methods:null,favs:["seven","removed"],pcs:["d"],spend:{seven:-10},pointPrio:{},ai:{empty:{}}});
  assert.equal(run("state.favs.join()"),"seven");
  assert.equal(run("state.pcs.join()"),"d");
  assert.equal(run('spendOf("seven")'),3000);
  assert.equal(run('restore(); renderList(); document.querySelector("#storeList").innerHTML.includes("セブン")'),true);
  assert.equal(run('sourceLink("mailto:help@example.org","案内")'),"");
  assert.match(run('rows("rakuten_ichiba").find(r=>r.m.id==="rakuten_card").src'),/event\.rakuten\.co\.jp/);
});
test("store conditions and benefit units affect both rank and review",()=>{
  const run=app({methods:["smbc_nl","paypay_card","cash"],pcs:["d","ponta"],favs:["lawson"]});
  assert.equal(run('bestPointCard(shop("lawson")).rate'),0.5);
  assert.equal(run('state.scenario.lawson="evening"; bestPointCard(shop("lawson")).rate'),1);
  assert.equal(run('state.scenario.starbucks="mobile"; rows("starbucks")[0].rate'),7);
  assert.equal(run('state.methods.push("sbux_card","rakuten_card"); rows("starbucks").map(r=>r.m.id).join()'),"smbc_nl,sbux_card,rakuten_card,paypay_card");
  assert.equal(run('rows("yahoo").find(r=>r.m.id==="paypay_card").rate'),2);
  assert.equal(run('state.scenario.yahoo="linked"; rows("yahoo").find(r=>r.m.id==="paypay_card").rate'),5);
  assert.equal(run('state.scenario.okstore="member_food"; amountLabel(rows("okstore")[0],1000)'),"約29円引き");
});
test("every saved store renders and a full wallet can be simulated",()=>{
  const run=app();
  assert.equal(run('state.methods=METHODS.map(m=>m.id); state.favs=STORES.map(s=>s.id); state.pcs=POINT_CARDS.map(p=>p.id); renderFavs(); renderList(); renderMethods(); Number.isFinite(suggest().cur.total)'),true);
});
test("dated online charge is used only until the documented end date",()=>{
  const run=app({methods:["jcb_w","sbux_card"],favs:["starbucks"],spend:{starbucks:10000}});
  assert.equal(run('rank(shop("starbucks"),activeMethods(),"2027-01-12")[0].rate'),10.5+STAR);
  assert.equal(run('rank(shop("starbucks"),activeMethods(),"2027-01-13").find(r=>r.m.charge).rate'),STAR);
  assert.equal(run('amountLabel(rank(shop("starbucks"),activeMethods(),"2027-01-12")[0],1000)'),"約105pt＋Star 16個");
  run('dateJST=()=>"2026-11-23"');
  const expected=23871*0.105+120000*STAR/100;
  assert.ok(Math.abs(run('simulate(activeMethods(),[shop("starbucks")]).total')-expected)<0.001);
});

test("Rakuten Ichiba adds weekday and 5/0-day bonuses only on those dates and not in the review",()=>{
  const run=app({methods:["rakuten_card","rakuten_premium"]});
  const r=(id,d)=>run(`rank(shop("rakuten_ichiba"),activeMethods(),"${d}").find(x=>x.m.id==="${id}").rate`);
  assert.equal(r("rakuten_premium","2026-11-23"),3);   // 月曜
  assert.equal(r("rakuten_premium","2026-11-26"),4);   // 木曜
  assert.equal(r("rakuten_card","2026-11-26"),3);
  assert.equal(r("rakuten_card","2026-11-30"),4);      // 30日
  assert.equal(r("rakuten_premium","2026-12-10"),5);   // 木曜かつ10日
  assert.equal(run('rank(shop("rakuten_ichiba"),activeMethods(),"2026-12-10",true).find(x=>x.m.id==="rakuten_premium").rate'),3);
});
