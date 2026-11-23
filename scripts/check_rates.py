#!/usr/bin/env python3
"""還元率の自動チェック

1. Claude が各決済の公式情報・ニュースを調べ、data.json との違い（変更点）を探す
2. ChatGPT が出典ページを自分で読み、Claude の結論を見ずに還元率を答える
3. 機械チェック（還元率の範囲・公式ドメインか・適用日）
4. 2つのAIが一致し、機械チェックも通った変更だけを data.json に反映
   それ以外は「要確認」としてレポート（Pull Request の本文）に載せる

あわせて、各お店で提示できる共通ポイントカード（dポイント・楽天・Ponta・V）と
その付与率の変化（導入・終了・率の変更）も、同じ「Claudeで調査 → ChatGPTで独立確認」の流れでチェックする。

必要な環境変数: ANTHROPIC_API_KEY, OPENAI_API_KEY
任意: ANTHROPIC_MODEL, OPENAI_MODEL（モデル名の上書き）, ONLY（決済IDをカンマ区切りで指定）,
      ONLY_STORES（お店IDをカンマ区切りで指定）, SKIP_STORES=1（お店のチェックを省略）
"""
import datetime
import json
import os
import re
import sys
import urllib.parse

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data.json")
REPORT_PATH = os.path.join(ROOT, "check_report.md")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or "gpt-5.6"
MAX_RATE = 20.0      # これを超える還元率は常設の還元率としては不自然なので要確認
TOLERANCE = 0.05     # 2つのAIの答えの許容差（%ポイント）
UA = "Mozilla/5.0 (compatible; otoku-pay-rate-checker; +https://github.com/sgtkszk7/Otoku_pay)"

JST = datetime.timezone(datetime.timedelta(hours=9))
TODAY = datetime.datetime.now(JST).date()


# ---------------------------------------------------------------- 共通処理

def fetch_text(url, limit=20000):
    """ページ本文をテキストで取得する。失敗したら空文字。"""
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding or r.encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n"))
        return text.strip()[:limit]
    except Exception:
        return ""


def extract_json(text):
    """AIの返答から最後のJSONオブジェクトを取り出す。"""
    if not text:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in reversed(candidates):
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def call_claude(prompt, use_search=True):
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    for _ in range(4):
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=300)
        r.raise_for_status()
        d = r.json()
        if d.get("stop_reason") == "pause_turn":
            # 長い検索で一時停止したときは、そのまま続きを依頼する
            body["messages"] = [body["messages"][0], {"role": "assistant", "content": d["content"]}]
            continue
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    return ""


def call_openai(prompt, use_search=False):
    headers = {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        "Content-Type": "application/json",
    }
    body = {"model": OPENAI_MODEL, "input": prompt}
    if use_search:
        body["tools"] = [{"type": "web_search"}]
    r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=body, timeout=300)
    r.raise_for_status()
    d = r.json()
    parts = []
    for item in d.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "".join(parts)


def host_of(url):
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""


def is_official(url, domains):
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def parse_date(s):
    try:
        return datetime.date.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 1. Claude で変更を探す

def claude_find_changes(method, data):
    stores = {s["id"]: s["name"] for s in data["stores"]}
    rules = [r for r in data["rules"] if r["method"] == method["id"]]
    current = {
        "基本還元率(%)": method["base"],
        "注意書き": method.get("note", ""),
        "お店ごとの特典": [
            {"store_id": r["store"], "お店": stores.get(r["store"], r["store"]),
             "合計還元率(%)": r["rate"], "内容": r["label"]} for r in rules
        ],
    }
    page = fetch_text(method["src"]) if method.get("src") else ""
    prompt = f"""あなたはキャッシュレス決済のポイント還元率を調べる担当です。今日は{TODAY.isoformat()}です。

対象の支払い方法: {method['name']}（{method['kind']}）
公式ドメイン: {', '.join(method.get('official_domains', [])) or '不明'}

アプリに登録している現在のデータ:
{json.dumps(current, ensure_ascii=False, indent=2)}

アプリが扱うお店（store_id: 名前）:
{json.dumps(stores, ensure_ascii=False)}

参考: 公式ページ {method.get('src', '')} の本文（取得できた範囲）:
<page>
{page or '（取得できませんでした）'}
</page>

やること:
- 公式サイトのお知らせ・ニュースリリースや報道をWeb検索で確認し、現在のデータと比べて
  「常設の還元率・条件」に変更（改悪・改善・適用開始日の決まった変更予定）があるか調べてください。
- 1か月未満の期間限定キャンペーン、抽選、エントリー制の一時的な施策は対象外です。
- 還元率は、そのお店で支払ったときの合計（基本分を含む）を%で答えてください。
- 確信が持てないものは報告しないでください。出典は公式サイトを優先してください。
- store_id は上の一覧にあるものだけを使い、通常の加盟店での基本還元率なら null にしてください。

最後に、次の形のJSONだけを出力してください（変更がなければ changes は空の配列）:
```json
{{"changes": [
  {{"kind": "base" または "store_rate" または "note",
    "store_id": null または store_id,
    "new_rate": 数値 または null,
    "new_note": "注意書きを変える場合の新しい文" または null,
    "effective_date": "YYYY-MM-DD" または null,
    "source_url": "出典URL",
    "evidence": "根拠の要約（100字以内）"}}
]}}
```"""
    text = call_claude(prompt, use_search=True)
    parsed = extract_json(text)
    if parsed is None or not isinstance(parsed.get("changes"), list):
        raise ValueError("Claudeの返答からJSONを読み取れませんでした")
    return parsed["changes"]


# ---------------------------------------------------------------- 2. ChatGPT で独立に確認

def chatgpt_verify(method, store_name, change):
    """Claudeの答えを見せずに、出典ページから還元率を読み取らせる。"""
    url = change.get("source_url", "")
    page = fetch_text(url) if url else ""
    target = f"{store_name}で支払ったとき" if store_name else "通常の加盟店で支払ったとき（基本還元率）"
    instructions = f"""今日は{TODAY.isoformat()}です。
質問: 「{method['name']}」を{target}のポイント還元率（基本分を含む合計、%）はいくつですか？
適用開始日と主な条件も答えてください。
1か月未満の期間限定キャンペーンや抽選は含めず、常設の還元率で答えてください。
ページに書かれていないことは推測せず null にしてください。

次の形のJSONだけを出力してください:
{{"rate": 数値 または null, "effective_date": "YYYY-MM-DD" または null, "conditions": "主な条件（100字以内）", "found": true または false}}"""
    if page:
        prompt = f"{instructions}\n\n以下のページ（{url}）の記述だけをもとに答えてください:\n<page>\n{page}\n</page>"
        text = call_openai(prompt, use_search=False)
    else:
        prompt = f"{instructions}\n\nページ {url} を開いて、その記述をもとに答えてください。"
        text = call_openai(prompt, use_search=True)
    return extract_json(text) or {"rate": None, "found": False, "conditions": "（返答を読み取れませんでした）"}


# ---------------------------------------------------------------- 5. お店のポイントカード

def pc_label(data, pcs):
    names = {c["id"]: c["name"] for c in data["point_cards"]}
    return "、".join(f"{names.get(x['id'], x['id'])} {x['rate']}%" for x in pcs) or "なし"


def current_pcs(store, data):
    rates = store.get("pc_rates", {})
    return [{"id": pid, "rate": rates.get(pid, data["point_card_rate"])} for pid in store.get("pc", [])]


def normalize_pcs(items, valid_ids):
    out = {}
    for x in items or []:
        pid = x.get("id") if isinstance(x, dict) else None
        rate = x.get("rate") if isinstance(x, dict) else None
        if pid in valid_ids and isinstance(rate, (int, float)) and 0 < rate <= 5:
            out[pid] = float(rate)
    return out


def claude_check_store(store, data):
    ids = [c["id"] for c in data["point_cards"]]
    cards = {c["id"]: c["name"] for c in data["point_cards"]}
    current = current_pcs(store, data)
    prompt = f"""あなたは日本のお店のポイントサービスを調べる担当です。今日は{TODAY.isoformat()}です。

対象のお店: {store['name']}（{store['cat']}・実店舗）
お店の公式ドメイン: {', '.join(store.get('official_domains', [])) or '不明'}
調べる共通ポイントカード（id: 名前）: {json.dumps(cards, ensure_ascii=False)}

アプリに登録している現在のデータ（会計時に提示して貯まるカードと付与率%）:
{json.dumps(current, ensure_ascii=False)}

やること:
- Web検索で、お店の公式サイトやポイント運営会社の公式発表を優先して確認し、
  上のカードを「会計時に提示して貯められるか」と「通常の付与率(%)」を調べてください。
  例: 200円につき1ポイントなら0.5、100円につき1ポイントなら1.0。
- 導入・終了・付与率の変更など、現在のデータとの違いがあるかを判断してください。
- 一部店舗だけの対応や、1か月未満の期間限定キャンペーンは「対応」に含めないでください。
- 確信が持てない場合は changed を false にしてください。

最後に、次の形のJSONだけを出力してください:
```json
{{"changed": true または false,
  "point_cards": [{{"id": "d", "rate": 0.5}}],
  "effective_date": "YYYY-MM-DD" または null,
  "source_url": "出典URL",
  "evidence": "根拠の要約（100字以内）"}}
```
point_cards には、調べた結果「今提示できる」カードをすべて入れてください（変更がなくても全部）。"""
    text = call_claude(prompt, use_search=True)
    parsed = extract_json(text)
    if parsed is None or "changed" not in parsed:
        raise ValueError("Claudeの返答からJSONを読み取れませんでした")
    parsed["point_cards"] = normalize_pcs(parsed.get("point_cards"), ids)
    return parsed


def chatgpt_verify_store(store, data, url):
    """Claudeの答えを見せずに、出典ページから提示できるカードと付与率を読み取らせる。"""
    ids = [c["id"] for c in data["point_cards"]]
    cards = {c["id"]: c["name"] for c in data["point_cards"]}
    page = fetch_text(url) if url else ""
    instructions = f"""今日は{TODAY.isoformat()}です。
質問: 「{store['name']}」の実店舗で、会計時に提示して貯められる共通ポイントカードはどれですか？
候補（id: 名前）: {json.dumps(cards, ensure_ascii=False)}
それぞれの通常の付与率(%)も答えてください（200円につき1ポイントなら0.5、100円につき1ポイントなら1.0）。
一部店舗だけの対応や期間限定キャンペーンは含めず、書かれていないことは推測しないでください。

次の形のJSONだけを出力してください:
{{"point_cards": [{{"id": "d", "rate": 0.5}}], "found": true または false, "note": "補足（100字以内）"}}"""
    if page:
        text = call_openai(f"{instructions}\n\n以下のページ（{url}）の記述だけをもとに答えてください:\n<page>\n{page}\n</page>")
    else:
        text = call_openai(f"{instructions}\n\nページ {url} を開いて、その記述をもとに答えてください。", use_search=True)
    parsed = extract_json(text) or {}
    return {"point_cards": normalize_pcs(parsed.get("point_cards"), ids),
            "found": bool(parsed.get("found")), "note": str(parsed.get("note", ""))[:120]}


def store_source_ok(url, store, data):
    domains = list(store.get("official_domains", []))
    for c in data["point_cards"]:
        domains += c.get("official_domains", [])
    return is_official(url, domains)


def judge_store(store, data, found, verify):
    reasons = []
    if not store_source_ok(found.get("source_url", ""), store, data):
        reasons.append("出典がお店かポイント運営会社の公式サイトではありません")
    if not verify.get("found"):
        reasons.append("ChatGPTが出典からポイントカードの情報を読み取れませんでした")
    else:
        a, b = found["point_cards"], verify["point_cards"]
        if set(a) != set(b):
            reasons.append("ChatGPTの読み取り（使えるカード）と一致しません")
        elif any(abs(a[k] - b[k]) > TOLERANCE for k in a):
            reasons.append("ChatGPTの読み取り（付与率）と一致しません")
    if reasons:
        return "review", reasons
    eff = parse_date(found.get("effective_date"))
    if eff and eff > TODAY:
        return "scheduled", [f"{eff.isoformat()}から適用予定"]
    return "apply", []


def apply_store(store, data, pcs):
    order = [c["id"] for c in data["point_cards"]]
    store["pc"] = [pid for pid in order if pid in pcs]
    rates = {pid: r for pid, r in pcs.items() if abs(r - data["point_card_rate"]) > TOLERANCE}
    if rates:
        store["pc_rates"] = rates
    else:
        store.pop("pc_rates", None)


def check_stores(data, results, errors):
    only = [x.strip() for x in os.environ.get("ONLY_STORES", "").split(",") if x.strip()]
    checked = 0
    for store in data["stores"]:
        if only and store["id"] not in only:
            continue
        if store.get("cat") == "ネット通販":
            continue  # ネット通販はポイントカードの提示がないので対象外
        try:
            found = claude_check_store(store, data)
        except Exception as e:
            errors.append(f"{store['name']}（ポイントカード）: 調査に失敗しました（{type(e).__name__}: {e}）")
            continue
        checked += 1
        before = {x["id"]: x["rate"] for x in current_pcs(store, data)}
        after = found["point_cards"]
        same = set(before) == set(after) and all(abs(before[k] - after[k]) <= TOLERANCE for k in after)
        if not found.get("changed") or same:
            continue
        try:
            verify = chatgpt_verify_store(store, data, found.get("source_url", ""))
        except Exception as e:
            verify = {"point_cards": {}, "found": False, "note": f"（ChatGPTの確認に失敗: {type(e).__name__}）"}
        decision, reasons = judge_store(store, data, found, verify)
        entry = {"store": store, "before": [{"id": k, "rate": v} for k, v in before.items()],
                 "after": [{"id": k, "rate": v} for k, v in after.items()],
                 "verify": [{"id": k, "rate": v} for k, v in verify["point_cards"].items()],
                 "verify_note": verify.get("note", ""), "found": found, "reasons": reasons}
        if decision == "apply":
            apply_store(store, data, after)
        results[decision].append(entry)
    return checked


# ---------------------------------------------------------------- 3〜4. 判定と反映

def judge(method, change, verify):
    """'apply'（反映） / 'scheduled'（予定） / 'review'（要確認） と理由を返す。"""
    reasons = []
    kind = change.get("kind")
    if kind == "note":
        return "review", ["注意書きの変更は自動では反映しません"]
    new_rate = change.get("new_rate")
    if not isinstance(new_rate, (int, float)):
        return "review", ["新しい還元率が数値ではありません"]
    if not (0 <= new_rate <= MAX_RATE):
        reasons.append(f"還元率が想定範囲（0〜{MAX_RATE}%）の外です")
    if not is_official(change.get("source_url", ""), method.get("official_domains", [])):
        reasons.append("出典が公式サイトではありません")
    g = verify.get("rate")
    if not isinstance(g, (int, float)):
        reasons.append("ChatGPTが出典から還元率を読み取れませんでした")
    elif abs(g - new_rate) > TOLERANCE:
        reasons.append(f"ChatGPTの読み取り（{g}%）と一致しません")
    if reasons:
        return "review", reasons
    eff = parse_date(change.get("effective_date"))
    if eff and eff > TODAY:
        return "scheduled", [f"{eff.isoformat()}から適用予定"]
    return "apply", []


def apply_change(data, method, change):
    if change["kind"] == "base":
        old = method["base"]
        method["base"] = change["new_rate"]
        return old
    store = change.get("store_id")
    for r in data["rules"]:
        if r["method"] == method["id"] and r["store"] == store:
            old = r["rate"]
            r["rate"] = change["new_rate"]
            return old
    data["rules"].append({
        "method": method["id"], "store": store, "rate": change["new_rate"],
        "label": "公式特典", "note": change.get("evidence", ""),
    })
    return None


def current_value(data, method, change):
    if change.get("kind") == "base":
        return method["base"]
    if change.get("kind") == "note":
        return method.get("note", "")
    for r in data["rules"]:
        if r["method"] == method["id"] and r["store"] == change.get("store_id"):
            return r["rate"]
    return None


def fmt_rate(v):
    return "なし" if v is None else (f"{v}%" if isinstance(v, (int, float)) else str(v))


# ---------------------------------------------------------------- メイン

def main():
    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)
    stores = {s["id"]: s["name"] for s in data["stores"]}
    only = [x.strip() for x in os.environ.get("ONLY", "").split(",") if x.strip()]

    results = {"apply": [], "scheduled": [], "review": []}
    errors = []
    checked = []

    for method in data["methods"]:
        if method["id"] == "cash" or (only and method["id"] not in only):
            continue
        try:
            changes = claude_find_changes(method, data)
        except Exception as e:
            errors.append(f"{method['name']}: 調査に失敗しました（{type(e).__name__}: {e}）")
            continue
        checked.append(method["name"])
        for ch in changes:
            store_id = ch.get("store_id")
            if store_id is not None and store_id not in stores:
                results["review"].append((method, ch, {}, ["アプリにないお店のIDです"], None))
                continue
            store_name = stores.get(store_id) if store_id else None
            before = current_value(data, method, ch)
            if ch.get("kind") != "note" and isinstance(before, (int, float)) and before == ch.get("new_rate"):
                continue  # 実は変わっていない
            try:
                verify = chatgpt_verify(method, store_name, ch)
            except Exception as e:
                verify = {"rate": None, "conditions": f"（ChatGPTの確認に失敗: {type(e).__name__}）"}
            decision, reasons = judge(method, ch, verify)
            if decision == "apply":
                apply_change(data, method, ch)
            results[decision].append((method, ch, verify, reasons, before))

    store_results = {"apply": [], "scheduled": [], "review": []}
    stores_checked = 0
    if os.environ.get("SKIP_STORES") != "1" and not (only and not os.environ.get("ONLY_STORES")):
        stores_checked = check_stores(data, store_results, errors)

    has_changes = any(results.values()) or any(store_results.values())
    if results["apply"] or store_results["apply"]:
        data["updated"] = TODAY.isoformat()
        data["checked_label"] = f"{TODAY.year}年{TODAY.month}月確認"
    if has_changes:
        # 要確認だけのときも Pull Request で知らせられるよう、チェック日を記録する
        data["last_auto_check"] = TODAY.isoformat()
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    report = build_report(results, errors, checked, stores, store_results, stores_checked, data)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"has_changes={'true' if has_changes else 'false'}\n")
            f.write(f"date={TODAY.isoformat()}\n")
    print(report)
    # 全件失敗（APIキーの設定ミスなど）のときはワークフローを失敗にして気づけるようにする
    if errors and not checked and not stores_checked:
        sys.exit(1)


def build_report(results, errors, checked, stores, store_results=None, stores_checked=0, data=None):
    store_results = store_results or {"apply": [], "scheduled": [], "review": []}
    def line(method, ch, verify, reasons, before):
        store = stores.get(ch.get("store_id")) if ch.get("store_id") else "通常の加盟店"
        if ch.get("kind") == "note":
            change = f"注意書き → {ch.get('new_note')}"
        else:
            change = f"{fmt_rate(before)} → **{fmt_rate(ch.get('new_rate'))}**"
        rows = [
            f"### {method['name']}（{store}）",
            f"- 変更: {change}",
            f"- 適用日: {ch.get('effective_date') or '不明'}",
            f"- 出典: {ch.get('source_url') or 'なし'}",
            f"- Claudeの根拠: {ch.get('evidence', '')}",
            f"- ChatGPTの読み取り: {fmt_rate(verify.get('rate'))}"
            + (f"（{verify.get('conditions')}）" if verify.get("conditions") else ""),
        ]
        if reasons:
            rows.append(f"- 判定理由: {' / '.join(reasons)}")
        return "\n".join(rows)

    parts = [f"# 還元率の自動チェック（{TODAY.isoformat()}）", ""]
    parts.append(f"調べた支払い方法: {len(checked)}件、お店のポイントカード: {stores_checked}店（Claude: `{ANTHROPIC_MODEL}` / ChatGPT: `{OPENAI_MODEL}`）")
    parts.append("")
    sections = [
        ("apply", "✅ 反映した変更（2つのAIが一致・公式の出典あり）",
         "data.json に反映済みです。内容を確認して問題なければ Merge してください。"),
        ("scheduled", "🕒 適用予定の変更（まだ反映していません）",
         "適用日を過ぎたあとのチェックで、改めて反映対象になります。"),
        ("review", "⚠️ 要確認（自動では反映していません）",
         "AI同士の答えが食い違ったか、出典が公式でないなどの理由があります。出典を見て、必要なら手で data.json を直してください。"),
    ]
    for key, title, desc in sections:
        items = results[key]
        if not items:
            continue
        parts += [f"## {title}", "", desc, ""]
        parts += [line(*it) + "\n" for it in items]
    store_titles = {"apply": "✅ お店のポイントカード：反映した変更", "scheduled": "🕒 お店のポイントカード：適用予定",
                    "review": "⚠️ お店のポイントカード：要確認（自動では反映していません）"}
    for key in ["apply", "scheduled", "review"]:
        items = store_results[key]
        if not items:
            continue
        parts += [f"## {store_titles[key]}", ""]
        for it in items:
            f = it["found"]
            rows = [f"### {it['store']['name']}",
                    f"- 変更: {pc_label(data, it['before'])} → **{pc_label(data, it['after'])}**",
                    f"- 適用日: {f.get('effective_date') or '不明'}",
                    f"- 出典: {f.get('source_url') or 'なし'}",
                    f"- Claudeの根拠: {f.get('evidence', '')}",
                    f"- ChatGPTの読み取り: {pc_label(data, it['verify'])}" + (f"（{it['verify_note']}）" if it.get("verify_note") else "")]
            if it["reasons"]:
                rows.append(f"- 判定理由: {' / '.join(it['reasons'])}")
            parts += ["\n".join(rows) + "\n"]
    if not any(results.values()) and not any(store_results.values()):
        parts += ["変更は見つかりませんでした。", ""]
    if errors:
        parts += ["## ❌ 調べられなかったもの", ""] + [f"- {e}" for e in errors] + [""]
    parts += ["---", "このPull Requestは GitHub Actions の「還元率の自動チェック」が作成しました。"]
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    main()
