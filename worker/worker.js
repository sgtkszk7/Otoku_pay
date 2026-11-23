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

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0],
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Vary": "Origin",
    };
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (request.method !== "POST") return json({ error: "POSTで呼び出してください" }, 405, cors);
    if (!ALLOWED_ORIGINS.includes(origin)) return json({ error: "許可されていない呼び出し元です" }, 403, cors);
    if (!env.ANTHROPIC_API_KEY) return json({ error: "APIキーが設定されていません" }, 500, cors);

    let body;
    try { body = await request.json(); } catch { return json({ error: "リクエストの形式が正しくありません" }, 400, cors); }
    const store = String(body.store || "").normalize("NFKC").trim().replace(/\s+/g, " ");
    if (!store || store.length > 40) return json({ error: "お店の名前は1〜40文字で入力してください" }, 400, cors);

    // キャッシュ確認（同じお店なら API を呼ばない）
    const cache = caches.default;
    const cacheKey = new Request("https://otoku-pay-cache.invalid/lookup?s=" + encodeURIComponent(store.toLowerCase()));
    const hit = await cache.match(cacheKey);
    if (hit) {
      const r = new Response(hit.body, hit);
      for (const [k, v] of Object.entries(cors)) r.headers.set(k, v);
      r.headers.set("X-Cache", "HIT");
      return r;
    }

    let data;
    try {
      data = await (await fetch(DATA_URL, { cf: { cacheTtl: 3600 } })).json();
    } catch {
      return json({ error: "還元率データを読み込めませんでした" }, 502, cors);
    }
    const methods = data.methods
      .filter((m) => m.id !== "cash" && !m.only_stores)
      .map((m) => ({ id: m.id, name: m.name, kind: m.kind, base: m.base }));

    let result;
    try {
      const { text, seenUrls } = await askClaude(env, buildPrompt(store, methods));
      result = sanitize(extractJson(text), methods, seenUrls, store);
      // 対象外の範囲のお店（地元のお店など）は、調べた中身を返さない
      const allow = (env.ALLOW_SCOPES || "national").split(",").map((x) => x.trim());
      if (result.found && !allow.includes(result.chain_scope)) {
        result = { found: true, blocked: true, query: store, store_name: result.store_name,
                   chain_scope: result.chain_scope, allowed_scopes: allow, checked_at: result.checked_at };
      }
    } catch (e) {
      return json({ error: "AIでの調査に失敗しました。時間をおいてもう一度お試しください。" }, 502, cors);
    }

    const resp = json(result, 200, { ...cors, "Cache-Control": `public, max-age=${CACHE_SECONDS}` });
    if (result.found) ctx.waitUntil(cache.put(cacheKey, resp.clone()));
    return resp;
  },
};

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

async function askClaude(env, prompt) {
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
    if (typeof s.url === "string" && /^https?:\/\//.test(s.url) && seenUrls.has(s.url)) {
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
