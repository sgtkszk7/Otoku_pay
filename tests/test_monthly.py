import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_rates as rates
import official_page


class MonthlyCheck(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(rates.DATA.read_text())
        self.task = "method-paypay"
        self.method = rates.subject(self.data, self.task)
        for k in ("variants", "note", "charge", "only_stores", "cap"):
            self.method.pop(k, None)  # 実データの注記に左右されないよう素の支払い方法で検証
        self.change = dict(kind="base", store_id=None, card_id=None, found=True, new_rate=0.8,
                           effective_date="2025-01-01", conditions="なし", evidence="公式の変更", source_url=self.method["src"])

    def judge(self, c=None, v=None):
        return rates.judge(self.data, self.task, c or self.change, v or self.change, "公式の変更")[0]

    def test_note_requires_review(self):
        self.method["note"] = "本人確認が必要"
        self.assertEqual(self.judge(), "review")

    def test_valid_scoped_write(self):
        self.assertEqual(self.judge(), "apply")
        rates.apply(self.data, self.task, self.change, self.method["src"])
        self.assertEqual(self.method["base"], 0.8)
        scoped = dict(self.change, kind="store_rate", store_id="starbucks", new_rate=0.1)
        self.assertEqual(self.judge(scoped, scoped), "apply")
        rates.apply(self.data, self.task, scoped, self.method["src"])
        self.assertTrue(rates.store_rule(self.data, "paypay", "starbucks")["override"])
        self.assertEqual(self.method["base"], 0.8)

    def test_invalid_records_never_reach_verification_or_write(self):
        proposals = [None, 7, [], dict(self.change, store_id="seven"), dict(self.change, kind="store_rate"),
                     dict(self.change, kind="store_rate", store_id="amazon"), dict(self.change, kind="other"),
                     dict(self.change, store_id={}), dict(self.change, new_rate=True), dict(self.change, new_rate=float("nan"))]
        before = copy.deepcopy(self.data)
        with patch.object(rates, "research", return_value=proposals), patch.object(rates,"fetch_page") as fetch:
            result = rates.investigate(self.data, self.task)
        fetch.assert_not_called()
        self.assertEqual([r["decision"] for r in result["rows"]], ["review"]*len(proposals))
        self.assertEqual(self.data,before)

    def test_scope_conditions_confirmation_date_and_quote(self):
        for fields in ({"found":False},{"conditions":"要登録"},{"effective_date":"不明"},
                       {"effective_date":"2099-01-01"},{"new_rate":True},{"kind":"store_rate"},{"evidence":"不明"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.judge(v=dict(self.change, **fields)), "review")
        future=dict(self.change,effective_date="2099-01-01")
        self.assertEqual(self.judge(future,future),"scheduled")

    def test_card_changes_cannot_replace_a_list(self):
        task="store-famima"
        store=rates.subject(self.data, task)
        before=list(store["pc"])
        c=dict(self.change,kind="point_end",card_id="d",store_id=store["id"],new_rate=None)
        for bad in [dict(c,card_id=None),dict(c, point_cards=[],card_id="other")]:
            self.assertIsNotNone(rates.scope_issue(self.data,task,bad))
        self.assertEqual(rates.judge(self.data,task,c,dict(c,evidence="別の本文"),"公式の変更")[0],"review")
        self.assertEqual(rates.judge(self.data,task,c,c,"公式の変更")[0],"review")
        c["evidence"]="dポイントカードの提示による付与を終了"
        self.assertEqual(rates.judge(self.data,task,c,c,c["evidence"])[0],"apply")
        self.assertEqual(store["pc"],before)
        url="https://"+store["official_domains"][0]+"/"
        rates.apply(self.data,task,c,url)
        self.assertEqual(store["pc"],[x for x in before if x!="d"])

    def test_conditional_rule_and_scoped_source(self):
        task="method-rakuten_card"
        c=dict(self.change,kind="store_rate",store_id="rakuten_ichiba")
        self.assertIn("event.rakuten.co.jp", rates.allowed_domains(self.data,task,c))
        self.assertNotIn("event.rakuten.co.jp", rates.allowed_domains(self.data,task,self.change))
        self.assertEqual(rates.judge(self.data,task,c,c,"公式の変更")[0],"review")

    def test_source_checks_precede_connection(self):
        with patch.object(official_page.socket,"getaddrinfo") as dns:
            for url in ["http://example.org","https://other.example","https://example.org:8443"]:
                with self.assertRaises(ValueError):
                    official_page.fetch_page(url,["example.org"])
            dns.assert_not_called()
        with patch.object(official_page.socket,"getaddrinfo",return_value=[(2,1,6,"",("127.0.0.1",443))]), \
                patch.object(official_page.socket,"create_connection") as connect:
            with self.assertRaises(ValueError):
                official_page.fetch_page("https://example.org",["example.org"])
            connect.assert_not_called()

    def test_redirect_is_checked_again_and_page_size_is_bounded(self):
        redirect = MagicMock(status=302)
        redirect.getheader.side_effect=lambda key: "https://other.example/destination" if key=="Location" else None
        too_big = MagicMock(status=200)
        too_big.headers.get_content_type.return_value="text/html"
        too_big.read.return_value=b"a"*1_000_001
        for response in [redirect, too_big]:
            with self.subTest(status=response.status), patch.object(official_page.socket,"getaddrinfo",
                    return_value=[(2,1,6,"",("93.184.216.34",443))]) as dns, \
                    patch.object(official_page.socket,"create_connection"), \
                    patch.object(official_page.ssl,"create_default_context"), \
                    patch.object(official_page.http.client,"HTTPSConnection") as conn:
                conn.return_value.getresponse.return_value=response
                with self.assertRaises(ValueError):
                    official_page.fetch_page("https://example.org",["example.org"])
                self.assertEqual(dns.call_count,1)

    def test_partial_collection_reports_failure_without_touching_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(rates,"ROOT",root),patch.object(rates,"DATA",root/"data.json"):
                result=dict(task=self.task,errors=[],rows=[],researched=1,verified=0)
                self.assertEqual(rates.aggregate(self.data,[self.task,"store-lawson"],{self.task:result}),1)
                self.assertIn("未完了",(root/"check_report.md").read_text())
                self.assertFalse((root/"data.json").exists())
