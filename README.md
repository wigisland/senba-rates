# senba-rates

SENBA Research の仮想通貨追跡レポート用・日次終値（JPY）データ置き場。

中身は `~/Documents/調査資料/レート/` の `rates.py` と `rates.json` をそのまま持ってきたもの。
これまで `update_rates.sh` + launchd がやっていた「Kraken取得 → 検証 → 公開」を
GitHub Actions に移し、毎日 01:00 UTC（10:00 JST）に自動実行する。

Mac が寝ていても動く。Gist ではなくリポジトリに commit する。

## Claude から読む

`gist.githubusercontent.com` はサンドボックスの許可ドメイン外だが、
`raw.githubusercontent.com` は許可されていて API のレート制限も無い。

```bash
curl -sS -o rates.json \
  "https://raw.githubusercontent.com/wigisland/senba-rates/main/rates.json"
```

取得したら `senba-report` スキルの `scripts/rates.json` に上書きして使う。

## 手動更新

Actions の「update-rates」→ Run workflow。ローカルなら従来どおり:

```bash
python3 rates.py update --coins BTC,ETH,SOL,USDT,XRP,TRX,USDC,DOGE,USD
python3 rates.py coverage
```

## 注意

- Kraken は 1リクエスト **720本** まで。約2年より前は API で埋まらないので、
  `rates.json` を上書きせずマージすること（`rates.py` の `merge()` は実装済み。
  BTC/ETH が 2024-01-03 まで遡れているのはこの履歴の蓄積による）。
- 720本より前が必要な場合は `rates.py import` で手元のCSVを取り込む。
- JPY直接ペアがあるのは **BTC / SOL / USDT / USD** のみ。ETH・XRP・TRX・USDC・DOGE は
  `COIN/USD × USD/JPY` の合成値。レポートの脚注表現はこれに合わせること。
- XRP/JPY は Kraken に板が無い（`XRPJPY`・`XXRPZJPY` とも Invalid）。
  `rates.py` は `Invalid asset pair` を捕捉して USD 合成に落ちる。
