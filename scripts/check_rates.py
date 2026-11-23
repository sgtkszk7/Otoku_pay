#!/usr/bin/env python3
"""Monthly research, independent verification, and scoped data updates."""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from official_page import fetch_page, is_official

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
TODAY = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or "gpt-5.6"


def post_json(url, headers, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


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
        d = post_json("https://api.anthropic.com/v1/messages", headers, body)
        if d.get("stop_reason") == "pause_turn":
            # 長い検索で一時停止したときは、そのまま続きを依頼する
            body["messages"] = [body["messages"][0], {"role": "assistant", "content": d["content"]}]
            continue
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    raise ValueError("調査の継続回数を超えました")


def call_openai(prompt, use_search=False):
    headers = {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        "Content-Type": "application/json",
    }
    body = {"model": OPENAI_MODEL, "input": prompt}
    if use_search:
        body["tools"] = [{"type": "web_search"}]
    d = post_json("https://api.openai.com/v1/responses", headers, body)
    parts = []
    for item in d.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "".join(parts)


def target_ids(data):
    return (["method-" + m["id"] for m in data["methods"] if m["id"] != "cash"]
            + ["store-" + s["id"] for s in data["stores"] if s["cat"] != "ネット通販"])


def subject(data, task):
    group, key = task.split("-", 1)
    return next(x for x in data["methods" if group == "method" else "stores"] if x["id"] == key)


def research(data, task):
    obj = subject(data, task)
    stores = {s["id"]: s["name"] for s in data["stores"]}
    if task.startswith("method-"):
        rules = [r for r in data["rules"] if r["method"] == obj["id"]]
        context = {"method": obj, "rules": rules, "stores": stores}
        kinds = "base（全店共通、store_id=null）、store_rate（特定店舗の合計率、store_id必須）、note（注意書き）"
    else:
        context = {"store": obj, "point_cards": data["point_cards"], "default_rate": data["point_card_rate"]}
        kinds = "point_rate（提示率の新設・変更）、point_end（このカードの提示特典終了）。店舗IDと個別のcard_idが必須。掲載漏れを終了と解釈しない"
    prompt = f"""今日は{TODAY}です。日本のポイント特典の常設条件について公式サイトを調査し、
登録データと違う項目だけを報告してください。店舗限定・支払方法限定・時間帯限定・条件付きの区別を保持。
対象と現行データ: {json.dumps(context, ensure_ascii=False)}
種別: {kinds}
返答はJSONオブジェクト。changesは変更候補の配列。確認して変更なしの場合は空配列。
各候補に kind, store_id, card_id（不要ならnull）, new_rate（合計%、終了ならnull）, found（確認できた真偽値）,
effective_date（YYYY-MM-DDまたはnull）, conditions（無条件の標準率のみ「なし」、ほかは具体的な条件）,
source_url, evidence（変更・終了を直接示す公式本文の短い引用）を入れてください。"""
    result = extract_json(call_claude(prompt))
    if not isinstance(result, dict) or not isinstance(result.get("changes"), list):
        # 返答の形が崩れることがあるので、1回だけ聞き直す
        result = extract_json(call_claude(prompt + "\n\n必ず {\"changes\": [...]} の形のJSONオブジェクトだけを出力してください。"))
    if not isinstance(result, dict) or not isinstance(result.get("changes"), list):
        raise ValueError("調査結果の変更一覧が欠けています")
    return result["changes"]


def finite_rate(value, maximum=20):
    return type(value) in (int, float) and 0 <= value <= maximum


def scope_issue(data, task, c):
    if not isinstance(c, dict) or "store_id" not in c:
        return "対象項目が不正です"
    kind, store, card = c.get("kind"), c["store_id"], c.get("card_id")
    obj = subject(data, task)
    if task.startswith("method-"):
        if card is not None or kind not in ("base", "store_rate"):
            return "決済の種別または対象が不明です"
        if kind == "base" and store is not None:
            return "全店共通の率に店舗が指定されています"
        if kind == "store_rate":
            if not isinstance(store, str) or not any(s["id"] == store for s in data["stores"]):
                return "店舗別の率には既知の店舗が必要です"
            if (obj["id"] in data["not_available"].get(store, [])
                    or ("only_stores" in obj and store not in obj["only_stores"])):
                return "利用できない組み合わせです"
    elif (kind not in ("point_rate", "point_end") or store != obj["id"]
          or not any(p["id"] == card for p in data["point_cards"])):
        return "ポイントカード単位の変更・終了を特定できません"
    if c.get("found") is not True or not isinstance(c.get("evidence"), str) or not c["evidence"].strip():
        return "変更の根拠が不明です"
    if kind == "point_end":
        if "new_rate" not in c or c["new_rate"] is not None:
            return "終了時に還元率が指定されています"
    elif not finite_rate(c.get("new_rate"), 5 if kind == "point_rate" else 20):
        return "還元率が範囲外または数値ではありません"
    return None


def store_rule(data, method, store):
    return next((r for r in data["rules"] if r["method"] == method and r["store"] == store), None)


def allowed_domains(data, task, c):
    obj = subject(data, task)
    domains = list(obj.get("official_domains", []))
    if task.startswith("method-"):
        rule = store_rule(data, obj["id"], c["store_id"])
        if rule and rule.get("src"):
            domains.append(urlsplit(rule["src"]).hostname)
    else:
        card = next(x for x in data["point_cards"] if x["id"] == c["card_id"])
        domains.extend(card.get("official_domains", []))
    return [d for d in domains if d]


def verify_page(data, task, c, url, page):
    obj = subject(data, task)
    stores = {s["id"]: s["name"] for s in data["stores"]}
    target = {"対象": obj["name"], "照会店舗": stores.get(c["store_id"]), "照会カードID": c.get("card_id")}
    prompt = f"""今日は{TODAY}です。対象 {json.dumps(target, ensure_ascii=False)} について、
次の公式ページ本文だけから対象範囲・通常還元率またはカード提示終了・開始日・条件を独立に読み取ってください。
店舗限定・条件付きの記述を共通率として扱わず、書かれていない内容はnull。
JSON形式: found（真偽値）, kind（base/store_rate/point_rate/point_end）, store_id（全店共通ならnull、店舗名とIDの対応は下記）,
card_id（決済率ならnull）, new_rate（数値、終了ならnull）, effective_date（YYYY-MM-DD）,
conditions（無条件の標準率のみ「なし」）, evidence（変更・終了を直接示す本文の引用）。
店舗: {json.dumps(stores, ensure_ascii=False)}
出典 {url}
<page>{page}</page>"""
    v = extract_json(call_openai(prompt))
    if not isinstance(v, dict):
        raise ValueError("照合結果の形式が不正です")
    return v


def judge(data, task, c, v, page):
    issue = scope_issue(data, task, c) or scope_issue(data, task, v)
    if issue:
        return "review", issue
    for key in ("kind", "store_id", "card_id", "new_rate", "effective_date", "conditions"):
        if c.get(key) != v.get(key):
            return "review", f"調査と照合で {key} が一致しません"
    if any(x["evidence"].strip() not in page for x in (c, v)):
        return "review", "公式本文に該当する証拠を確認できません"
    if c["kind"] == "point_end":
        card = next(p for p in data["point_cards"] if p["id"] == c["card_id"])
        name = card["name"].removesuffix("カード").casefold()
        for claim in (c, v):
            quote = claim["evidence"].casefold()
            if name not in quote or not any(word in quote for word in ("終了", "廃止", "取りやめ")):
                return "review", "該当カードの提示特典終了を明記した根拠が必要です"
    date = c.get("effective_date")
    try:
        effective = datetime.date.fromisoformat(date)
        if effective.isoformat() != date:
            raise ValueError
    except (TypeError, ValueError):
        return "review", "適用日を確認できません"
    if effective > TODAY:
        return "scheduled", f"{effective}から適用予定"
    obj = subject(data, task)
    if c.get("conditions") != "なし":
        return "review", "条件付きの変更です"
    if c["kind"] == "store_rate" and store_rule(data, obj["id"], c["store_id"]):
        return "review", "既存ルールの表示文・払い方・上限を合わせて確認してください"
    if c["kind"] == "base" and any(obj.get(k) for k in ("variants", "note", "charge", "only_stores", "cap")):
        return "review", "カードの表示文・利用条件も合わせて確認してください"
    if c["kind"].startswith("point_") and obj.get("pc_periods"):
        return "review", "時間帯別の提示率も確認してください"
    return "apply", ""


def apply(data, task, c, url):
    if scope_issue(data, task, c) or not is_official(url, allowed_domains(data, task, c)):
        raise ValueError("反映先または検証済み出典が不正です")
    obj = subject(data, task)
    kind, rate = c["kind"], c["new_rate"]
    if kind == "base":
        obj.update(base=rate, src=url)
    elif kind == "store_rate":
        if store_rule(data, obj["id"], c["store_id"]):
            raise ValueError("既存ルールは要確認です")
        data["rules"].append({"method": obj["id"], "store": c["store_id"], "rate": rate,
                              "label": "店舗別還元率", "override": True, "src": url})
    else:
        card = c["card_id"]
        rates = obj.setdefault("pc_rates", {})
        if kind == "point_end":
            obj["pc"] = [k for k in obj["pc"] if k != card]
            rates.pop(card, None)
        else:
            if card not in obj["pc"]:
                obj["pc"].append(card)
            rates[card] = rate
        if not rates:
            obj.pop("pc_rates")


def investigate(data, task):
    result = {"task": task, "researched": 0, "verified": 0, "errors": [], "rows": []}
    try:
        changes = research(data, task)
        result["researched"] = 1
    except Exception as e:
        result["errors"].append(f"調査失敗 {type(e).__name__}: {e}")
        return result
    for c in changes:
        issue = scope_issue(data, task, c)
        row = {"change": c, "decision": "review", "reason": issue}
        result["rows"].append(row)
        if issue:
            continue
        if sum(isinstance(x, dict) and x.get("store_id") == c["store_id"]
                              and x.get("card_id") == c.get("card_id") for x in changes) > 1:
            row["reason"] = "同一対象の候補が重複しています"
            continue
        domains = allowed_domains(data, task, c)
        if not is_official(c.get("source_url"), domains):
            row["reason"] = "許可された公式HTTPS出典ではありません"
            continue
        try:
            url, page = fetch_page(c["source_url"], domains)
            v = verify_page(data, task, c, url, page)
            result["verified"] += 1
            decision, reason = judge(data, task, c, v, page)
            row.update(decision=decision, reason=reason, url=url, verification=v)
        except Exception as e:
            row["reason"] = f"検証失敗 {type(e).__name__}: {e}"
            result["errors"].append(row["reason"])
    return result


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def aggregate(data, tasks, results):
    original = json.dumps(data, sort_keys=True)
    counts = {k: 0 for k in ("researched", "verified", "errors")}
    lines = [f"# 還元率チェック（{TODAY}）", ""]
    candidates = False
    for task in tasks:
        result = results.get(task)
        if result is None or result["task"] != task:
            result = {"errors": ["未完了"], "rows": [], "researched": 0, "verified": 0}
        for k in ("researched", "verified"):
            counts[k] += result[k]
        counts["errors"] += len(result["errors"])
        name = subject(data, task)["name"]
        for error in result["errors"]:
            lines.append(f"- {name} — {error}")
        for row in result["rows"]:
            candidates = True
            c = row["change"]
            if row["decision"] == "apply":
                apply(data, task, c, row["url"])
            lines.extend([f"### {name}（{row['decision']}）", row["reason"] or "公式本文と独立照合済み",
                          json.dumps(c, ensure_ascii=False), row.get("url", ""), ""])
    if json.dumps(data, sort_keys=True) != original:
        data.update(updated=TODAY.isoformat(), checked_label=f"{TODAY.year}年{TODAY.month}月確認")
    if candidates:
        data["last_auto_check"] = TODAY.isoformat()
        dump(DATA, data)
    lines.insert(2, f"対象 {len(tasks)} 件・調査完了 {counts['researched']} 件・照合完了 {counts['verified']} 件・エラー {counts['errors']} 件\n")
    report = "\n".join(lines) + "\n"
    (ROOT / "check_report.md").write_text(report, encoding="utf-8")
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"has_changes={str(candidates).lower()}\ndate={TODAY}\n")
    return 1 if counts["errors"] else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--target")
    parser.add_argument("--aggregate", type=Path)
    args = parser.parse_args()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    tasks = target_ids(data)
    # 手動実行で対象を絞る（例: ONLY_TARGETS="method-rakuten_card,store-seven"）。空なら全件
    picked = set(filter(None, (x.strip() for x in os.getenv("ONLY_TARGETS", "").split(","))))
    if picked:
        unknown = picked - set(tasks)
        if unknown:
            parser.error("unknown target: " + ", ".join(sorted(unknown)))
        tasks = [t for t in tasks if t in picked]
    if args.list:
        print(json.dumps(tasks))
        return 0
    if args.target:
        if args.target not in tasks:
            parser.error("unknown target")
        result = investigate(data, args.target)
        dump(Path(f"{args.target}.json"), result)
        return int(bool(result["errors"]))
    if args.aggregate:
        results = {t: json.loads(p.read_text()) for t in tasks if (p := args.aggregate / f"{t}.json").is_file()}
    else:
        only = set(filter(None, os.getenv("ONLY", "").split(",")))
        stores = set(filter(None, os.getenv("ONLY_STORES", "").split(",")))
        tasks = [t for t in tasks if (t.startswith("method-") and (not only or t[7:] in only) and (only or not stores))
                 or (t.startswith("store-") and os.getenv("SKIP_STORES") != "1"
                     and (not only or stores) and (not stores or t[6:] in stores))]
        results = {t: investigate(data, t) for t in tasks}
    return aggregate(data, tasks, results)


if __name__ == "__main__":
    sys.exit(main())
