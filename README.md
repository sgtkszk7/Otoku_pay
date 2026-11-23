# どれ払い？

いちばん得する払い方がすぐわかる。

ふだん使っている支払い方法（クレジットカード、QR決済、タッチ決済など）とよく行くお店を登録すると、どの支払い方法が一番ポイント還元でおトクかを調べられるWebアプリです。

## 機能

- **お店**：登録済みの全店をジャンル別に一覧表示し、各お店で一番おトクな支払い方法（1位）を表示。タップで詳しいランキング。上部のジャンルボタンでその場所へスライド。☆を押したお店は「よく行くお店」として一番上に表示。店名で絞り込み、リストにないお店はAIで調べられる（GitHub Pages版のみ）
- **見直し**：よく行くお店ごとに月の利用額といつもの払い方を入れると、おすすめとの差（+◯円）や、手持ちの使い分け・新しく追加すると増える支払い方法を試算
- **支払い方法**：使っている決済にチェック。会員ランクなどに合わせて還元率を自分で上書き可能。スタバカードはチャージ方法も選べる
- **ポイントカード**：dポイント・楽天・Ponta・Vポイントカードの提示分を上乗せして計算

データは利用者のブラウザ（localStorage）にのみ保存されます。カード番号などは扱いません。

## ホーム画面への追加

`manifest.webmanifest` と `assets/` のアイコンで、ホーム画面に「どれ払い？」のアイコンで追加できます。
アイコンの元データは `icons/icon.svg`（通常）と `icons/icon-maskable.svg`（Android の切り抜き用に余白を広くしたもの）です。

## 使い方

`index.html` 1ファイルだけで動きます。ブラウザで直接開くか、GitHub Pagesで公開してください。

## データについて

還元率は2026年11月時点で公式サイト等を確認した目安です（プロトタイプ：主要チェーン34店・決済20種）。対象店舗・条件・上限・キャンペーンは頻繁に変わるため、実際の還元は各社の公式情報で確認してください。

データは `data.json` で管理しています。

- `methods`：支払い方法（基本還元率・注意書き・出典・公式ドメイン・年会費 `annual_fee_yen`・月の対象上限 `monthly_cap_yen`）
- `stores`：お店（名前・別名・使えるポイントカード）
- `rules`：お店ごとの特典（そのお店での合計還元率）

## 還元率の自動チェック

`.github/workflows/rate-check.yml` が毎月1日 9:17（日本時間）に `scripts/check_rates.py` を実行します。Actions タブから手動でも実行できます。

1. **Claude** が各決済の公式お知らせ・ニュースを調べ、`data.json` との違いを探す
2. **ChatGPT** が出典ページを自分で読み、Claude の結論を見ずに還元率を答える
3. **機械チェック**：還元率が0〜20%か、出典が公式サイトか、適用日を過ぎているか
4. すべて一致した変更だけ `data.json` に反映し、**Pull Request** を作成。食い違いなどは「要確認」として本文に記載

あわせて、**お店のポイントカード**（dポイント・楽天・Ponta・Vの導入・終了・付与率の変更）も同じ流れでチェックします。出典は、お店かポイント運営会社の公式サイト（`data.json` の `official_domains`）のときだけ自動で反映します。ネット通販は対象外です。

- 決済だけ・お店だけを調べたいとき：`ONLY=paypay,smbc_nl`（決済ID）、`ONLY_STORES=mos,famima`（お店ID）、`SKIP_STORES=1`（お店を省略）

Merge すると GitHub Pages に自動で反映されます。

### 必要な設定

- Secrets（Settings → Secrets and variables → Actions）：`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`
- Settings → Actions → General →「Allow GitHub Actions to create and approve pull requests」をオン
- Anthropic Console で Web 検索を有効化
- 任意の Variables：`ANTHROPIC_MODEL`（既定 `claude-sonnet-5-5`）、`OPENAI_MODEL`（既定 `gpt-5.6`）

## AI検索の中継プログラム（Cloudflare Workers）

`worker/worker.js` は、アプリから店名を受け取り、Claude（Web検索つき）で調べて結果を返す中継プログラムです。APIキーをアプリ（誰でも見られる）に置かないために使います。

- 呼び出せるのは `https://sgtkszk7.github.io` からだけ
- 同じお店の結果は7日間キャッシュ（API代の節約）
- AIの返答は、還元率0〜20%・既知の支払い方法・実際に検索したURLだけに絞ってから返す
- 調べられるのは全国チェーンのみ。AIが「地域チェーン」「地元のお店」と判断した場合は中身を返さず、アプリに注意のポップアップを出す（Workerの変数 `ALLOW_SCOPES` を `national,regional` にすると地域チェーンも対象）

### 設定手順

1. Cloudflare のダッシュボード →「Workers & Pages」→「Create」→「Start with Hello World!」で Worker を作る
2.「Edit code」で `worker/worker.js` の中身に置き換えて「Deploy」
3. Worker の「Settings」→「Variables and Secrets」で `ANTHROPIC_API_KEY` を **Secret** として追加
4. 表示された Worker の URL（`https://〜.workers.dev`）を `data.json` の `lookup_endpoint` に入れる

## 今後の予定

- 対象店舗リストの公式情報との突き合わせ
- 店舗・キャンペーンデータの拡充
- AIで調べたお店を「よく行くお店」に登録できるようにする
