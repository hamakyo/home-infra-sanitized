# 隔離 AI エージェント実行基盤の設計（OpenCode）

状態： 構築済み（Step 1〜3 完了：`ubuntu-desktop` で OpenCode 1.19.6 隔離コンテナ・対話利用・読み取り専用定時ジョブ 3 本が稼働中）

`ubuntu-desktop` 上で既存の管理基盤（Caddy・Zabbix・NetBox・Fleet Free・Authentik）と安全に同居させながら、夜間バッチや調査・リポジトリ補助を回すための隔離 AI エージェント実行基盤の設計。選定経緯と落とした案は [隔離された常駐 AI エージェントのランタイムに opencode serve / opencode run を選んだ](decisions/0008-agent-runtime-opencode.md) を参照。

※ 現況（稼働中）は [自宅デバイスの現況](devices.md) と [自宅ネットワークの現況](network.md) を参照。設計と現況を混同しないこと。

## 構成図

![隔離 AI エージェント実行基盤の構成（Step 1 稼働中）：管理者（Mac / Windows・Tailscale）から Caddy TLS（Basic 認証）経由またはホスト側タイマー（薄いラッパー）経由で ubuntu-desktop の隔離 Docker コンテナ（OpenCode 1.19.6：非 root・read_only・mem_limit 1g・docker.sock なし）へ接続。内部の opencode.json で対話用（ask）と夜間バッチ用（読み取り限定・破壊コマンド deny）を分離し、主系のさくらの AI Engine（DeepSeek-V4.1-Flash）と予備の OpenCode 無料枠へ外向き通信。Zabbix / NetBox へは HTTPS 読み取り専用 API のみ接続し、DB ネットワーク・他スタック .env とは完全分離。コンテナ稼働は既存 zabbix-agent2 で監視し、バッチ結果は Discord Webhook へ通知](diagrams/agent-runtime.png)

- 自己完結 HTML：[diagrams/agent-runtime.html](diagrams/agent-runtime.html)（`caru-ini/stack-diagram` で生成）

## 役割分担と起動モード

OpenCode はコーディング・シェル実行ハーネスとして使い、常駐チャットやスケジューリングは外側の薄い仕組みへ分離する。

| モード | 実行方式 | 用途 | 権限モデル（`opencode.json`） |
| --- | --- | --- | --- |
| **対話モード** | `macOS ノート PC`（macOS）・`workstation-01`（Windows）から Tailscale 経由で `opencode serve` へ `opencode attach` 等で接続 | リポジトリ調査、設定差分・Ansible 下書き、手動検証 | `read` は許可、`edit` / `bash` は原則 `ask`（都度承認）、危険コマンド・`*.env` は `deny` |
| **夜間バッチモード** | ホスト側タイマー等から `opencode run --format json` を起動 | 監視・台帳の読み取り要約、定期チェック、レポート生成 | `read` / `glob` / `grep` と許可済み読み取りコマンドのみ `allow`、`edit` / `rm` / `git push` / `*.env` は `deny` |
| **通知・定期起動（外側）** | systemd timer + 薄いラッパースクリプト | 実行回数上限・タイムアウト制御、終了コード記録、Discord Webhook 通知 | LLM を持たない決定論的処理のみ |

## LLM プロバイダー構成

モデル ID・エンドポイントは `opencode.json` に外出しし、無料枠の枯渇やプレビュー提供終了時に即差し替えられるようにする。

| 区分 | プロバイダー | エンドポイント / 方式 | モデル例 | 主な制約・守り |
| --- | --- | --- | --- | --- |
| **主系** | **さくらの AI Engine**（無償プラン） | `https://api.ai.sakura.ad.jp/v1`（`@ai-sdk/openai-compatible`） | `preview/DeepSeek-V4.1-Flash-1017`（代替：`gpt-oss-120b`） | 月 3,000 リクエスト上限（超過時はレート制限のみで自動課金なし）、国内完結・ZDR、プロンプトキャッシュ非対応、連続呼び出し時 429 注意 |
| **予備** | **OpenCode Console（旧 Zen）**（クライアント向け無料枠） | OpenCode 内蔵プロバイダー | `opencode/deepseek-v4.1-flash-free` / `opencode/glm-5.3-flash-free` 等 | 公式クライアント内から利用可能。日次上限非公開・モデル入替が早いため予備扱い（ZDR 条件のモデルのみ選定） |

## 5 層のガードレール設計

OpenCode はデフォルトのファイル操作・シェル実行能力が強いため、`ubuntu-desktop` 上の本番基盤（Authentik・Zabbix・NetBox・Fleet）を守る 5 層の境界を必須要件とする。

### 1. コンテナ隔離・リソース上限
- **非 root 実行**：専用非 root UID（`10001:10001`）で起動する（公式 Alpine イメージの root デフォルトをそのまま使わない）
- **最小ケーパビリティ**：`cap_drop: [ALL]`、`security_opt: [no-new-privileges:true]`、`read_only: true`（書き込み許可は専用ワークスペース `/data` と `tmpfs: /tmp` のみ）
- **絶対マウント禁止**：`/var/run/docker.sock`、他スタックの `stacks/*/.env`、PostgreSQL / MySQL 等の DB ボリュームは一切マウントしない
- **リソース上限**：`mem_limit: 1g`、`cpus: 2.0`、`pids_limit: 256` を明示し、OOM やプロセス暴走が他基盤へ波及するのを防ぐ
- **バージョン固定**：OpenCode を `1.19.6` に明示固定し、`OPENCODE_DISABLE_AUTOUPDATE=true` でコンテナ内の自動更新を無効化する

### 2. ネットワークと入口の制限
- **入口の統一（Caddy + Tailscale 閉域）**：生ポート `4096` はホスト側へ直接公開せず、既存の Caddy リバースプロキシ配下 **`https://opencode.home.example.com`**（Tailscale 閉域・`*.home.example.com` ワイルドカード TLS）に集約する。`macOS ノート PC`・`workstation-01` の双方から同じ URL で到達できる
- **認証**：**`OPENCODE_SERVER_PASSWORD`**（HTTP Basic 認証）を必須化する（Authentik Forward Auth を前段に置くとブラウザ認証リダイレクトで CLI の `opencode attach` と干渉しやすいため、CLI 併用時は Tailscale 閉域＋Basic 認証を本線とする）
- **専用ネットワーク分離**：Caddy と OpenCode だけを繋ぐ **`opencode-proxy`** と、Read-Only RAG MCP サーバー（`infra-rag-mcp`）だけを繋ぐ **`rag-mcp-net`** のみに所属させ、Zabbix・NetBox・Fleet・Authentik・`infra-dwh-postgres` の内部 DB ネットワークおよび `proxy` ネットワークから完全分離する

### 3. OpenCode 内部の権限ルール（`opencode.json`）
- `opencode.json` は `:ro` でマウント（`OPENCODE_CONFIG=/etc/opencode/opencode.json`）し、エージェント自身による設定改ざんを防ぐ
- グローバル設定で `"*": "ask"`（`batch` エージェントでは `"*": "deny"`）をベースにし、`*.env`・秘密鍵・認証ファイルへの `read` / `edit`、`rm -rf`・`git push`・`env`・`printenv` は明示的に `deny` とする
- 対話用エージェントと夜間バッチ用エージェント（`batch`）を明確に分け、無人実行でファイル変更や外部書き込みが走らない設計を徹底する

### 4. 秘密情報・最小権限トークン・枠制御
- **専用 `.env`（`chmod 600`・git 管理外）**：このコンテナ専用の最小権限トークンのみを渡す
  - さくらの AI Engine：無償枠アカウントトークン（自動課金なし）
  - GitHub：対象リポジトリのみに絞った Fine-grained PAT（初期は読み取り専用または PR 作成限定）
  - Zabbix / NetBox：読み取り専用 API トークン
- **無料枠の枯渇防止**：定期ポーリング（Heartbeat）は回さない。ラッパー側で 1 回あたりの実行タイムアウト（例：3〜5 分）と 1 日の起動回数上限を制御し、月 3,000 リクエスト枠を超えない運用にする

### 5. 監視・監査（Zabbix 連携）
- `ubuntu-desktop` 上で稼働中のネイティブ `zabbix-agent2` で `opencode` コンテナの稼働状態・CPU・メモリ消費・異常再起動を継続監視する
- `opencode run --format json` の出力ログと終了コードを専用ディレクトリへ保存し、異常終了時のみ Discord Webhook 通知（または Zabbix Trapper）へ流す

## 段階導入

1. **Step 1（隔離コンテナ基盤と最小疎通）— 完了**：[stacks/opencode/](../stacks/opencode/) に非 root（UID `10001`）・`read_only`・`mem_limit: 1g`・専用 `opencode-proxy` ネットワークの OpenCode `1.19.6` と `opencode.json` を構築し、`https://opencode.home.example.com`（無認証 `401` / Basic 認証 `200`）・他スタック隔離・`batch` エージェントによる主系（`sakura/preview/DeepSeek-V4.1-Flash-1017`）および予備（`opencode/deepseek-v4.1-flash-free`）の応答（`cost: 0`）まで確認済み
2. **Step 2（読み取り専用定時ジョブ 3 本の本番稼働）— 完了**：ホスト側の薄いラッパー群（[stacks/opencode/jobs/](../stacks/opencode/jobs/)：排他ロック・タイムアウト `120s`・1 日上限 `5` 回・主系失敗時の予備モデル自動フォールバック・構造化 JSON ログ 30 日保持）と、各基盤の最小権限読み取り専用 API トークン（Zabbix `opencode-ro`・NetBox `opencode-ro` v2 `write_enabled=False`・Fleet `opencode-ro@home.example.com` `observer`/`api_only=1`）を整備し、`ubuntu-desktop` の `cron` へ 3 本登録済み（合計約 38 回/月 ＝ さくらの AI Engine 無料枠 3,000 回の約 1.3%）
   - **毎日 `07:00 JST`**：[daily_health.py](../stacks/opencode/jobs/daily_health.py) — ホスト資源・Docker・Zabbix 障害・24h エラーログの日次ヘルス要約 → Discord
   - **毎週月曜 `08:00 JST`**：[weekly_inventory_cve.py](../stacks/opencode/jobs/weekly_inventory_cve.py) — Fleet 端末＆CVE ＋ NetBox 台帳の週次棚卸し要約 → Discord
   - **毎週日曜 `20:00 JST`**：[weekly_github_patrol.py](../stacks/opencode/jobs/weekly_github_patrol.py) — `example-user/home-infra-sanitized` の Open Issue / PR・直近 7 日コミット・`docs/` 状態の週次巡回要約 → Discord
3. **Step 3（対話利用・運用定着）— 稼働中**：`https://opencode.home.example.com` 経由での対話接続を確認済み（Phase 2 の NAS 導入時に移設するか、`ubuntu-desktop` 側に隔離サンドボックスとして残すかは運用実績を見て判断する）
4. **Step 4（ガバナンス付き Read-Only RAG MCP 連携）— 完了**：`stacks/opencode/compose.yaml` へ `rag-mcp-net` と `RAG_MCP_TOKEN`、`opencode.json` へリモート MCP `infra-rag`（`http://infra-rag-mcp:8080/mcp`・Bearer 認証・`infra-rag_*` 許可）を追加。`opencode` から `infra-dwh-postgres` への非到達（隔離維持）と、`opencode mcp list`（`infra-rag connected`）および `batch` エージェントからの出典付き知識検索・ライブ統制 KPI 取得を確認済み（詳細は [data-platform-design.md](data-platform-design.md)）

## 未決事項

- **Phase 2（NAS 導入後）の配置判断**：NAS 側へ移設するか、`ubuntu-desktop` 側に隔離サンドボックスとして残すかを運用実績で決める
- ※ 実行ホスト `ubuntu-desktop` のリソースは実測確認済み（`opencode` 含む全 17 コンテナ稼働中で空きメモリ available `4.5〜6.3 GiB`、`opencode` 単体消費約 `174 MiB`）
