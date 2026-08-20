# Jグランツ補助金検索 MCP — torchi PoC

デジタル庁の[Jグランツ公開API](https://developers.digital.go.jp/documents/jgrants/api/)を、NotionなどのMCPクライアントから利用するための読み取り専用リモートMCPです。

デジタル庁の公開サンプル [`digital-go-jp/jgrants-mcp-server`](https://github.com/digital-go-jp/jgrants-mcp-server) を基に、Sliplane常設運用向けの認証、ヘルスチェック、コンテナ構成、出力量制御を追加しています。

## 提供ツール

- `ping` — 接続確認
- `search_subsidies` — キーワード、地域、業種、従業員数、利用目的で検索
- `get_subsidy_overview` — 締切期間・上限金額別の集計
- `get_subsidy_detail` — 詳細と添付資料一覧を取得
- `get_file_content` — 選択した添付資料だけをMarkdown化

検索結果には公式JグランツURLと出典を付けます。取得情報は参考情報であり、申請前に公式ページの最新情報を確認してください。

## リモート構成

```text
Notion
  -> HTTPS + Bearer token
  -> Caddy gateway（公開）
  -> FastMCP server（Sliplane内部のみ）
  -> Jグランツ公開API
```

- `/health` は監視用として認証なしで公開します。
- `/mcp` はGatewayとMCP本体の両方で同じBearer tokenを検証します。
- TokenはSliplaneのSecret環境変数にだけ保存し、GitHubやログへ入れません。
- 添付資料はMCP本体の一時領域へ保存され、必要なファイルだけ本文を返します。

## ローカル開発

Python 3.10以上と[uv](https://docs.astral.sh/uv/)を使用します。

```bash
uv sync
uv run pytest
JGRANTS_REQUIRE_AUTH=0 uv run python -m jgrants_mcp_server
```

認証付きのDocker構成は次のとおりです。

```bash
cp .env.example .env
# .envのMCP_AUTH_TOKENを32文字以上のランダム値へ変更
docker compose up --build
curl http://localhost:8080/health
curl -i http://localhost:8080/mcp
```

未認証の `/mcp` はHTTP 401になります。

## Sliplane設定

同じGitHubリポジトリから2サービスを作成します。

### `jgrants-mcp`

- Dockerfile: `Dockerfile`
- 公開: オフ
- Healthcheck: `/health`
- 環境変数:
  - `JGRANTS_HOST=0.0.0.0`
  - `JGRANTS_PATH=/mcp`
  - `JGRANTS_REQUIRE_AUTH=1`
  - `MCP_AUTH_TOKEN=<secret>`
  - `PORT=8000`

### `jgrants-mcp-gateway`

- Dockerfile: `gateway/Dockerfile`
- 公開: オン
- Healthcheck: `/health`
- 環境変数:
  - `MCP_AUTH_TOKEN=<same secret>`
  - `UPSTREAM_URL=http://jgrants-mcp.internal:8000`

`main`へのpushを自動デプロイ対象にします。NotionにはGatewayの公開URLに `/mcp` を付けたURLとBearer tokenを登録します。

## ライセンスと出典

コードはMIT Licenseです。JグランツAPIの利用は[JグランツAPI利用規約](https://www.jgrants-portal.go.jp/api/terms)に従ってください。公開・転用時はJグランツを出典として明記してください。
