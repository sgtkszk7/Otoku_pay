/**
 * どれ払い？：リストにないお店をAIで調べる中継プログラム（Cloudflare Workers）
 *
 * アプリから {"store": "バーガーキング"} を POST すると、Claude（Web検索つき）が
 * そのお店で各支払い方法が使えるか・お店独自の上乗せがあるかを調べて JSON で返す。
 *
 * 設定（Cloudflare のダッシュボード → このWorker → Settings → Variables and Secrets）
 *   ANTHROPIC_API_KEY : Claude の APIキー（Secret として登録。コードには書かない）
 *   ANTHROPIC_MODEL   : 任意。使うモデル名（既定 claude-sonnet-5-5）
 *   ALLOW_SCOPES      : 任意。調べられるお店の範囲（既定 "national"）
 *                       national=全国チェーン, regional=地域チェーン, local=個人店など
 *                       例："national,regional" にすると地域チェーンも対象になる
 */

const DATA_URL = "https://sgtkszk7.github.io/Otoku_pay/data.json";
const ALLOWED_ORIGINS = ["https://sgtkszk7.github.io"];
const DEFAULT_MODEL = "claude-sonnet-5-5";
const CACHE_SECONDS = 7 * 24 * 60 * 60; // 同じお店は7日間、前回の結果を使い回す
const POINT_CARD_IDS = ["d", "rakuten", "ponta", "v"];
const SCOPES = ["national", "regional", "local", "unknown"];

const MAX_BODY = 2048;

function positiveLimit(value) {
  const n = Number(value);
  return Number.isSafeInteger(n) && n > 0 ? n : 0;
}

async function readLookup(request) {
  if (Number(request.headers.get("Content-Length")) > MAX_BODY || !request.body) return null;
  const reader = request.body.getReader(), bytes = new Uint8Array(MAX_BODY);
  let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      if (size + value.length > MAX_BODY) { await reader.cancel(); return null; }
      bytes.set(value, size);
      size += value.length;
    }
    const body = JSON.parse(new TextDecoder().decode(bytes.subarray(0, size)));
    if (typeof body?.store !== "string") return null;
    const store = body.store.normalize("NFKC").trim().replace(/\\s+/g, " ");
    return store && store.length <= 40 ? store : null;
  } catch { return null; }
}

async function identify(request, credentials) {
  const auth = request.headers.get("Authorization") || "";
  if (!/^Bearer [A-Za-z0-9_-]{32,128}$/.test(auth)) return null;
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(auth.slice(7)));
  const hex = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("");
  const users = JSON.parse(credentials);
  return Object.hasOwn(users, hex) && /^[a-zA-Z0-9_-]{1,64}$/.test(users[hex]) ? users[hex] : null;
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin");
    if (!ALLOWED_ORIGINS.includes(origin)) return json({error:"呼び出し元が許可されていません"}, 403);
    const cors = {"Access-Control-Allow-Origin":origin, "Access-Control-Allow-Methods":"POST, OPTIONS",
                  "Access-Control-Allow-Headers":"Content-Type, Authorization", "Vary":"Origin", "Cache-Control":"no-store"};
    if (request.method === "OPTIONS") return new Response(null, {status:204, headers:cors});
    if (request.method !== "POST") return json({error:"POSTで呼び出してください"}, 405, cors);
    if (!env.LOOKUP || !env.LOOKUP_USERS || !env.ANTHROPIC_API_KEY || !positiveLimit(env.USER_DAILY_CALLS)
        || !positiveLimit(env.MONTHLY_CALLS) || !positiveLimit(env.MONTHLY_BUDGET_CENTS)
        || !positiveLimit(env.CALL_RESERVE_CENTS)) return json({error:"AI検索は準備中です"}, 503, cors);
    try {
      const user = await identify(request, env.LOOKUP_USERS);
      if (!user) return json({error:"有効な利用コードを入力してください"}, 401, cors);
      const store = await readLookup(request);
      if (!store) return json({error:"店名を1〜40文字で指定してください"}, 400, cors);
      const coordinator = env.LOOKUP.get(env.LOOKUP.idFromName("paid-search"));
      const r = await coordinator.fetch("https://lookup.internal/", {method:"POST", body:JSON.stringify({user, store})});
      return new Response(r.body, {status:r.status, headers:{...cors, "Content-Type":"application/json; charset=utf-8"}});
    } catch {
      return json({error:"AI検索を利用できません"}, 503, cors);
    }
  },
};

class LimitReached extends Error {}

export class LookupCoordinator {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.pending = new Map();
  }

  async reserve(user) {
    const day = new Date().toISOString().slice(0, 10), month = day.slice(0, 7);
    const daily = "user:" + user, global = "monthly";
    await this.ctx.storage.transaction(async tx => {
      const previous = await tx.get([daily, global]);
      const count = (key, period) => previous.get(key)?.period === period ? previous.get(key).count : 0;
      const calls = count(daily, day), total = count(global, month);
      const reserved = previous.get(global)?.period === month ? previous.get(global).reservedCents : 0;
      const nextCost = reserved + positiveLimit(this.env.CALL_RESERVE_CENTS);
      if (!Number.isSafeInteger(nextCost) || nextCost > positiveLimit(this.env.MONTHLY_BUDGET_CENTS)) throw new LimitReached();
      if (calls >= positiveLimit(this.env.USER_DAILY_CALLS) || total >= positiveLimit(this.env.MONTHLY_CALLS))
        throw new LimitReached();
      await tx.put({[daily]:{period:day, count:calls+1}, [global]:{period:month, count:total+1, reservedCents:nextCost}});
    });
  }

  async fetch(request) {
    const {user, store} = await request.json();
    const key = (this.env.ALLOW_SCOPES || "national") + ":" + store.toLowerCase();
    if (!this.pending.has(key)) {
      const work = this.lookup(key, store, user).finally(() => this.pending.delete(key));
      this.pending.set(key, work);
    }
    try { return json(await this.pending.get(key), 200); }
    catch (e) {
      return e instanceof LimitReached ? json({error:"AI検索の利用上限に達しました"}, 429)
        : json({error:"AIでの調査に失敗しました"}, 502);
    }
  }

  async lookup(key, store, user) {
    const cacheKey = "cache:" + key;
    const hit = await this.ctx.storage.get(cacheKey);
    if (hit && hit.expires > Date.now()) return hit.result;
    const response = await fetch(DATA_URL, {cf:{cacheTtl:3600}});
    if (!response.ok) throw new Error("data unavailable");
    const data = await response.json();
    const methods = data.methods.filter(m => m.id !== "cash" && !m.only_stores);
    const {text, seenUrls} = await askClaude(this.env, buildPrompt(store, methods), () => this.reserve(user));
    let result = sanitize(extractJson(text), methods, seenUrls, store);
    const allow = (this.env.ALLOW_SCOPES || "national").split(",").map(x => x.trim());
    if (result.found && !allow.includes(result.chain_scope)) {
      result = {found:true, blocked:true, query:store, store_name:result.store_name,
                chain_scope:result.chain_scope, allowed_scopes:allow, checked_at:result.checked_at};
    }
    await this.ctx.storage.put(cacheKey, {result, expires:Date.now() + (result.found ? CACHE_SECONDS : 3600)*1000});
    if (await this.ctx.storage.getAlarm() === null) await this.ctx.storage.setAlarm(Date.now() + 86400000);
    return result;
  }

  async alarm() {
    let startAfter;
    do {
      const entries = await this.ctx.storage.list({prefix:"cache:", limit:100, ...(startAfter ? {startAfter} : {})});
      const expired = [...entries].filter(([,v]) => v.expires <= Date.now()).map(([k]) => k);
      if (expired.length) await this.ctx.storage.delete(expired);
      startAfter = entries.size === 100 ? [...entries.keys()].pop() : null;
    } while (startAfter);
    await this.ctx.storage.setAlarm(Date.now() + 86400000);
  }
}

function buildPrompt(store, methods) {
  const today = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
  const list = methods.map((m) => `- ${m.id}: ${m.name}（${m.kind}・通常${m.base}%）`).join("\n");
  return `今日は${today}です。日本の「${store}」の実店舗で、次の支払い方法が使えるかを調べてください。

支払い方法（id: 名前・種類・通常の還元率）:
${list}

ルール:
- Web検索で、お店の公式サイト（支払い方法の案内ページなど）や決済会社の公式情報を優先して確認してください。
- お店が特定できない、または日本に実店舗がない場合は found を false にしてください。
- chain_scope には、お店の展開範囲を入れてください。
  "national"=全国の複数の地方（例：関東と関西など）に店舗を展開するチェーン、
  "regional"=特定の地方や都道府県を中心に複数店舗を展開するチェーン、
  "local"=1〜数店舗の個人店・地元のお店、"unknown"=判断できない。
- 使えることが確認できたら usable は "yes"、使えないと確認できたら "no"、わからなければ "unknown"。
- そのお店で、通常より還元率が上がる常設の特典（特約店・パートナー店など）がある場合だけ、rate に合計の還元率(%)を入れてください。なければ rate は null です。
- 1か月未満の期間限定キャンペーン、抽選、エントリー制の一時的な施策は含めないでください。
- 提示してポイントが貯まる共通ポイントカードを point_cards に入れてください（d=dポイント, rakuten=楽天ポイント, ponta=Pontaポイント, v=Vポイント）。わからなければ空の配列。
- 推測で埋めないでください。

最後に、次の形のJSONだけを出力してください:
{"found": true, "store_name": "正式な店名", "category": "飲食・スーパーなど", "chain_scope": "national",
 "methods": [{"id": "支払い方法のid", "usable": "yes|no|unknown", "rate": null, "note": "補足（40字以内、なければ空）"}],
 "point_cards": [], "summary": "このお店の支払いのポイント（80字以内）",
 "sources": [{"title": "ページ名", "url": "https://..."}]}`;
}

async function askClaude(env, prompt, reserve) {
  const headers = {
    "x-api-key": env.ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
  };
  const body = {
    model: env.ANTHROPIC_MODEL || DEFAULT_MODEL,
    max_tokens: 3000,
    messages: [{ role: "user", content: prompt }],
    tools: [{ type: "web_search_20250305", name: "web_search", max_uses: 4 }],
  };
  const seenUrls = new Set();
  for (let i = 0; i < 3; i++) {
    await reserve();
    const r = await fetch("https://api.anthropic.com/v1/messages", { method: "POST", headers, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`Claude API ${r.status}`);
    const d = await r.json();
    for (const b of d.content || []) {
      if (b.type === "web_search_tool_result" && Array.isArray(b.content)) {
        for (const x of b.content) if (x.url) seenUrls.add(x.url);
      }
    }
    if (d.stop_reason === "pause_turn") {
      body.messages = [body.messages[0], { role: "assistant", content: d.content }];
      continue;
    }
    const text = (d.content || []).filter((b) => b.type === "text").map((b) => b.text).join("");
    return { text, seenUrls };
  }
  throw new Error("調査が長すぎました");
}

function extractJson(text) {
  const blocks = [...(text || "").matchAll(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/g)].map((m) => m[1]);
  const s = text.indexOf("{"), e = text.lastIndexOf("}");
  if (s !== -1 && e > s) blocks.push(text.slice(s, e + 1));
  for (const c of blocks.reverse()) {
    try { return JSON.parse(c); } catch { /* 次の候補 */ }
  }
  throw new Error("JSONを読み取れませんでした");
}

/** AIの返答を、アプリが安全に表示できる形に整える */
function sanitize(r, methods, seenUrls, store) {
  if (typeof r?.found !== "boolean") throw new Error("invalid search result");
  const ids = new Set(methods.map((m) => m.id));
  const str = (v, n) => (typeof v === "string" ? v.slice(0, n) : "");
  const out = {
    found: r.found === true,
    query: store,
    store_name: str(r.store_name, 40) || store,
    category: str(r.category, 20),
    chain_scope: SCOPES.includes(r.chain_scope) ? r.chain_scope : "unknown",
    summary: str(r.summary, 120),
    methods: [],
    point_cards: Array.isArray(r.point_cards) ? r.point_cards.filter((p) => POINT_CARD_IDS.includes(p)) : [],
    sources: [],
    checked_at: new Date().toISOString().slice(0, 10),
  };
  for (const m of Array.isArray(r.methods) ? r.methods : []) {
    if (!ids.has(m.id)) continue;
    const usable = ["yes", "no", "unknown"].includes(m.usable) ? m.usable : "unknown";
    const rate = typeof m.rate === "number" && m.rate >= 0 && m.rate <= 20 ? m.rate : null;
    out.methods.push({ id: m.id, usable, rate, note: str(m.note, 60) });
  }
  // 出典は、実際にWeb検索で見たURLだけを残す（AIが作ったURLを出さないため）
  for (const s of Array.isArray(r.sources) ? r.sources : []) {
    if (typeof s.url === "string" && /^https:\/\//.test(s.url) && seenUrls.has(s.url)) {
      out.sources.push({ title: str(s.title, 60) || s.url, url: s.url });
    }
    if (out.sources.length >= 5) break;
  }
  return out;
}

function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...headers },
  });
}
