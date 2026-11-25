# 自宅インフラ 統制・可視化データ基盤の設計（Infra Data & AI Hub）

状態： 構築済み（Step 1〜Step 3 完了：`ubuntu-desktop` で稼働中）

`ubuntu-desktop` 上で稼働中の Phase 1 管理基盤（Authentik・NetBox・Fleet Free・Zabbix・`restic` バックアップ）から運用・統制データを日次収集し、ツール間の資産・権限・脆弱性・バックアップ不整合を自動検知・可視化するデータ基盤の設計。

技術選定の経緯と落とした案は [Step 1 の決定記録](decisions/0010-data-platform-infra-stack.md)・[Step 2（RAG・MCP）の決定記録](decisions/0011-rag-pipeline-pgvector-sakura-mcp.md)・[Step 3（n8n 自動化）の決定記録](decisions/0012-workflow-automation-n8n.md) を参照。

## 構成図

![自宅インフラ 統制・可視化データ基盤＆自動化（Infra Data & AI Hub・Step 1〜3 稼働中）の構成：管理者（Tailscale 閉域）から Caddy + Authentik 経由で Grafana OSS 12.4（bi.home.example.com・OIDC SSO・3 画面 27 パネル）と n8n 2.48.7（n8n.home.example.com・Forward Auth・WF01 障害トリアージ / WF02 日次統制要約）へ接続。Phase 1 管理基盤（Authentik・NetBox・Fleet）および Zabbix 8.0 / 設計書 MD・restic 履歴から infra-dwh-pipeline（04:00 JST 日次 dlt / dbt / RAG）が読取専用収集し PostgreSQL 17（pgvector・4 DB ロール分離）へ格納。PostgreSQL から Grafana へ infra_bi_ro、n8n へ n8n_app / n8n_ro、infra-rag-mcp へ rag_reader で最小権限接続し、Zabbix Trapper へ 9 指標送信、Backblaze B2 へ restic 日次暗号化退避。infra-rag-mcp から隔離コンテナ OpenCode 1.19.6 へは専用 rag-mcp-net のみで DB 遮断を維持し、さくらの AI Engine（E5-large 埋込 / DeepSeek 要約・推論・国内 ZDR）と Discord Webhook へ連携](diagrams/data-platform.png)

- 自己完結 HTML：[diagrams/data-platform.html](diagrams/data-platform.html)（`caru-ini/stack-diagram` で生成）

## 目的と全体ロードマップ

複数の管理システムに散らばった情報を読み取り専用 API で名寄せし、**「管理元（SSoT）の突合」「データ品質テストによる統制チェック」「監査証跡とトレンドの可視化」** を 1 か所で回す。

- **Step 1（本設計の対象・構築済み）**：自宅インフラ 統合データマート＆監査証跡基盤（`PostgreSQL 17 (pgvector)` ＋ `dlt` ＋ `dbt-core` ＋ `Grafana OSS`）
- **Step 2（構築済み）**：同じ PostgreSQL（`pgvector`）上へ、設計書・ADR・Step 1 ライブマートを秘密情報マスク付きで取り込むガバナンス付き RAG パイプライン（SHA-256 増分同期・12 問ゴールデン評価・Grafana RAG ダッシュボード）および隔離 OpenCode 向け Read-Only MCP Gateway（`infra-rag-mcp`）を追加
- **Step 3（構築済み）**：`stacks/n8n/` に **`n8nio/n8n:2.48.7`**（Authentik Embedded Outpost Forward Auth＋専用 DB `n8n_app`＋Read-Only `infra-rag-mcp` 連携）を構築し、実行履歴の `dlt` 取込 → `dbt` ROI マート → Grafana 自動化効果ダッシュボードまで統合

## アーキテクチャ（Step 1）

### コンポーネント構成（`stacks/data-platform/`）

| コンポーネント | イメージ / 実装 | ネットワーク | リソース上限目安 | 役割 |
| --- | --- | --- | --- | --- |
| **`infra-dwh-postgres`** | `pgvector/pgvector:0.8.7-pg17-trixie`（PostgreSQL 17.12・`vector 0.8.7`） | `infra-dwh-net`（内部限定・ホストポート非公開） | `mem_limit: 768m` / `cpus: 1.5` | DWH DB（`infra_dwh`）と Grafana メタデータ DB（`grafana_app`）を収容 |
| **`infra-dwh-grafana`** | `grafana/grafana-oss:12.4.3` | `infra-dwh-net` ＋ `proxy` | `mem_limit: 512m` / `cpus: 1.0` | `https://bi.home.example.com`（Caddy＋Tailscale 閉域・Authentik OIDC SSO・Provisioning as Code） |
| **`infra-dwh-pipeline`** | Python 3.12 ＋ `dlt[postgres]` ＋ `dbt-postgres`（自作 `Dockerfile`） | `infra-dwh-net`（`profiles: ["job"]`・非定常駐） | `mem_limit: 768m` / `cpus: 1.5` | ホスト `cron` から日次ワンショット起動（待機メモリ `0 MiB`）。収集 → `dbt run` → `dbt test` → 結果通知 |

- **常駐メモリ増加目安**：PostgreSQL ＋ Grafana で約 `0.5〜1.0 GiB`（上限設定合計 `1.25 GiB`）。実測の `ubuntu-desktop` 空きメモリ `7.1 GiB`・SSD 空き `159 GiB` に対して十分な余力を残す。

### 収集対象データソース（5 系統・すべて読み取り専用）

| ソース | 取得経路 | 取得対象（例） | 認証・権限方針 |
| --- | --- | --- | --- |
| **Authentik** | `https://auth.home.example.com/api/v3/` | ユーザー一覧（有効状態・最終ログイン）、グループ、MFA 登録状況、監査イベント（ログイン成功/失敗・設定変更） | 専用読み取り専用サービスアカウント（`data-ro`）の App Token を新規発行 |
| **NetBox** | `https://netbox.home.example.com/api/` | デバイス、IP アドレス、インターフェース、ロール・ステータス | 読み取り専用 API トークン（`write_enabled=False`） |
| **Fleet Free** | `https://fleet.home.example.com/api/v1/fleet/` | ホスト一覧（OS・ディスク残量・`osquery` 状態・最終通信）、脆弱性（CVE・CVSS）、脆弱ソフトウェア | `observer` / `api_only=1` の読み取り専用トークン |
| **Zabbix 8.0 LTS** | `https://zabbix.home.example.com/api_jsonrpc.php` | 監視対象ホスト、アクティブ障害・トリガー、イベント履歴 | 読み取り専用 API トークン |
| **`restic` バックアップ実績** | `/home/infra-admin/.local/state/infra-backup/runs/*.json` | 日次バックアップ成否、DB 別ダンプ容量、所要時間、スナップショット ID | バッチコンテナへ `:ro` マウントして増分取り込み |

※ 各 API トークン・DB パスワード・OIDC シークレットはホスト上の `stacks/data-platform/.env`（`chmod 600`・git 管理外）にのみ保存し、リポジトリやドキュメントへ値を書かない。

## データモデル（Medallion 3 層）

PostgreSQL の `infra_dwh` データベース内を 3 スキーマへ分離する。

1. **`raw` スキーマ（取込層）**：`dlt` が各ソースの日次スナップショット（`snapshot_date` 付き）と監査イベント（追記・重複排除）をそのまま格納する
2. **`stg` スキーマ（正規化層）**：`dbt` で型変換・JST タイムスタンプ統一・ホスト名正規化（例：`laptop-01.local` / `workstation-01` / `ubuntu-desktop` の小文字化と共通キー付与）を行う
3. **`mart` スキーマ（公開・統制層）**：Grafana および将来の RAG / レポートが参照する集計・突合テーブル群

| マート名 | 主な内容 | 目的（統制・可視化） |
| --- | --- | --- |
| **`mart_asset_reconciliation`** | Fleet × NetBox × Zabbix の 3 点突合（ホスト単位） | 台帳未登録（シャドー端末）・監視漏れ・一定期間未通信端末の検出 |
| **`mart_identity_posture`** | Authentik ユーザー別の有効状態・MFA 有無・最終ログイン経過日数・失敗ログイン件数 | ID 棚卸し・MFA 未設定アカウントや異常認証の把握 |
| **`mart_vulnerability_posture`** | 端末 × CVE（CVSS・重大度・検出日・放置日数・対象ソフト） | 重要 CVE の対応優先度付けと減少トレンドの可視化 |
| **`mart_backup_compliance`** | `restic` バックアップ日次履歴（成否・直近成功からの経過時間・DB 容量推移） | バックアップ鮮度と容量増加の監査証跡 |
| **`mart_infra_kpi_daily`** | 日次統制サマリ（突合一致率・重要 CVE 数・MFA 適用率・バックアップ健全率） | 週次・月次の改善推移を 1 画面で追跡 |

## `dbt test` による IT 統制チェック

`dbt run` 直後に `dbt test` を実行し、データ品質と IT 統制ルールの両方を自動検証する。

- **データ品質テスト（`severity: error`）**：主キーの一意性（`unique`）・必須項目欠損（`not_null`）・ソース取込鮮度（24 時間以内に日次スナップショットが到達していること）
- **IT 統制ルールテスト（`severity: warn` / 重大項目は `error`）**：
  - Fleet に存在するが NetBox 台帳に未登録の端末が 0 件であること
  - NetBox で運用中の PC が Zabbix 監視対象に含まれていること
  - Authentik の通常有効ユーザーに MFA が設定されていること（Break-glass 等の例外ルールは明示管理）
  - 直近 36 時間以内に `restic` バックアップが `ok` で完了していること
  - 重大 CVE（例：CVSS 9.0 以上）の長期放置がないこと

実行結果（終了コード・所要時間・取込件数・`dbt test` の `pass / warn / fail` 件数）は JSON ログ（`/home/infra-admin/.local/state/infra-dwh/runs/`）へ保存し、**Zabbix Trapper** へ送信して障害・失敗時に既存の Discord 通知経路へ乗せる。

## セキュリティ・認証・運用設計

### 1. PostgreSQL ロールの最小権限分離
- **`etl_writer`**：`infra_dwh` の `raw` / `stg` / `mart` に対する読み書き（`infra-dwh-pipeline` 専用）
- **`bi_reader`**：`infra_dwh` の `mart` スキーマに対する `SELECT` のみ（Grafana データソース専用。`raw` 監査詳細や更新権限を持たせない）
- **`grafana_app`**：Grafana 内部設定 DB（`grafana_app`）のみアクセス可

### 2. HTTPS 公開と SSO（Caddy + Authentik）
- Caddy（`stacks/caddy/Caddyfile`）に `bi.home.example.com` を追加し、Tailscale 閉域の既存ワイルドカード TLS（`*.home.example.com`）配下へ収容する
- Authentik に OIDC プロバイダー・アプリケーション（`Infra BI`）を作成し、Grafana の `[auth.generic_oauth]` で SSO ログインを統合する（Break-glass 用ローカル管理者 1 アカウントを別途保持）

### 3. 定時スケジュール（既存ジョブとの非衝突設計）
- `01:30 JST`：既存 Phase 1 DB バックアップ（`stacks/backup/backup_phase1_db.py`）
- `03:00 JST`：既存 `unattended-upgrades` 自動再起動窓
- **`04:00 JST`（新設）**：Infra DWH 日次パイプライン（排他ロック・タイムアウト制御付きラッパー経由で `docker compose --profile job run --rm pipeline`）
- `07:00 JST`：既存 OpenCode 日次ヘルス要約

### 4. バックアップ連携
- 構築後、`infra-dwh-postgres` の `pg_dump` を `stacks/backup/backup_phase1_db.py` の退避対象へ組み込む（`mart` 定義と Grafana ダッシュボードは Git 正本、`raw` 履歴と Grafana 状態は日次 B2 退避で二重保全する）。

## 段階構築計画（Step 1）

1. **Step 1a（基盤と認証）— 完了**：[stacks/data-platform/](../stacks/data-platform/) に `infra-dwh-postgres`（`pgvector/pgvector:0.8.7-pg17-trixie`・`vector 0.8.7` 有効）と `infra-dwh-grafana`（`grafana/grafana-oss:12.4.3`）を構築し、Caddy（`https://bi.home.example.com`）・Authentik OIDC SSO（`infra-bi`）・専用読み取りサービスアカウント `data-ro`（GET `200` / POST `403`）・PostgreSQL ロール分離（`bi_reader` は `mart.*` のみ許可・`raw.*` は `permission denied`）を確認済み
2. **Step 1b（収集パイプライン）— 完了**：`infra-dwh-pipeline`（Python 3.12＋`dlt 1.20.0`＋`dbt-core 1.12.7` / `dbt-postgres 1.9.1`・非 root UID `1000`・`cap_drop: [ALL]`）を構築し、5 系統 10 テーブル（Authentik ユーザー 4 / MFA 0 / イベント 35・NetBox 8・Fleet ホスト 3 / CVE 200 / 脆弱ソフト 100・Zabbix ホスト 5 / 障害 1・`restic` 履歴 3）の冪等ロードを確認済み
3. **Step 1c（`dbt` モデリング＆統制テスト）— 完了**：ホスト名名寄せシード（`asset_hostname_aliases.csv`）・`stg` 5 ビュー・`mart` 6 テーブル（計 11 モデル `PASS=11`）と 16 データ/統制テストを確認済み（初期構築時は `PASS=15 WARN=1` で MFA 未設定 2 アカウントを自動検知 → 同日の Issue #2 MFA 完了・Authentik per-user MFA 収集修正後に **`PASS=16 WARN=0 ERROR=0`**・MFA 適用率 `100%` を確認）
4. **Step 1d（ダッシュボード＆運用統合）— 完了**：Grafana Provisioning（データソース `infra-dwh-pg` 正常・ダッシュボード `Infra Governance & Operations Overview` 11 パネル）を Git 管理で反映し、ホスト側 `run_pipeline.py`（実行時間 `17.47s`・`04:00 JST` `cron` 登録）・Zabbix Trapper 5 アイテム / 2 トリガー（`processed: 5; failed: 0`）・`stacks/backup/backup_phase1_db.py` への `infra_dwh` / `grafana_app` 追加（`pg_dump` 検証済み）まで完了。実測追加常駐メモリは約 `196 MiB`（PostgreSQL `61.5 MiB`＋Grafana `134.1 MiB`・全 19 コンテナ稼働中でホスト空きメモリ `6.8 GiB`・SSD 空き `156 GiB`）

## Step 2：ガバナンス付き RAG パイプライン ＆ Read-Only MCP Gateway（構築済み）

Step 1 の `infra-dwh-postgres`（`pgvector 0.8.7`）と日次バッチ `infra-dwh-pipeline` を拡張し、設計書・ADR・ライブ統制マートを安全に検索できる RAG 基盤と、隔離 OpenCode 専用の Read-Only MCP Gateway を構築した。

### 追加コンポーネントとネットワーク境界

| コンポーネント | イメージ / 実装 | ネットワーク | リソース上限・実測 | 役割 |
| --- | --- | --- | --- | --- |
| **`infra-rag-mcp`** | Python 3.12-slim + Starlette/Uvicorn（非 root UID `10001`・`read_only: true`・`cap_drop: [ALL]`） | `infra-dwh-net` ＋ `rag-mcp-net` | 上限 `256m` / 実測 `32.5 MiB` | Read-Only Streamable HTTP MCP サーバー（`POST /mcp`・Bearer 認証必須・`GET /health`）。`rag_reader`（`default_transaction_read_only=on`）で `rag.*` / `mart.*` のみ参照 |
| **`infra-dwh-pipeline`（Step 2 拡張）** | Step 1 イメージに `rag/`（`redact.py` / `search_lib.py` / `sync_rag.py` / `eval_rag.py`）を統合 | `infra-dwh-net`（非定常駐） | 上限 `768m` / 待機 `0 MiB` | `04:00 JST` の Step 1 完了直後に RAG 増分同期 → 12 問ゴールデン評価 → Zabbix Trapper 送信（計 9 指標）を一気通貫実行 |

- **PostgreSQL 最小権限ロール（`rag_reader`）**：`rag.*` と `mart.*` の `SELECT` のみ許可し、`raw.*` / `stg.*` / `public` と一切の書き込み（`INSERT` / `UPDATE` / `DELETE`）を拒否（実機テストで `permission denied` 確認済み）
- **隔離境界の維持**：`opencode` は `opencode-proxy` と **`rag-mcp-net`** のみに接続し、`infra-dwh-net` へは接続しない（`opencode` コンテナ内から `infra-dwh-postgres` が名前解決不可であることを実機検証済み）

### ガバナンス・同期・評価パイプライン（`rag` スキーマ）

1. **許可リスト方式のコーパス収集（`sync_rag.py`）**
   - 対象：`example-user/home-infra-sanitized` の `README.md`・`docs/*.md`・`docs/decisions/*.md`（計 24 文書）＋ Step 1 `mart.*` から生成するライブ運用サマリ 3 カード（資産 3 点突合・ID/MFA ポスチャー・バックアップ＆脆弱性サマリ）＝ 計 27 文書・331 チャンク
   - 除外：個人情報を含む別リポジトリ、`.env`、HTML 図版、その他許可リスト外のファイルは一切取り込まない
   - チャンク分割：`multilingual-e5-large` の 512 トークン上限に収めるため、見出し階層（`[タイトル > H2 / H3]`）を保持しつつ最大 500 文字・行単位で分割
2. **PII / 秘密情報マスクゲート（`redact.py`）**
   - 埋め込み生成前に決定論的正規表現で API キー・JWT・PEM 秘密鍵・Bearer トークン・メール・電話番号・パスワード代入文を自動マスクし、マスク件数を記録
3. **SHA-256 増分同期＆ハイブリッド検索（`search_lib.py`）**
   - 埋め込みモデル：**さくらの AI Engine 無償プラン `multilingual-e5-large`**（1024 次元・国内完結 ZDR・月 10,000 リクエスト無料・自動課金なし）
   - `rag.chunks.content_sha256` で既存チャンクと照合し、変更分のみ API 呼び出し（初回フル同期 299 チャンク＝19 API コール → 2 回目再実行時は `298/299` チャンクがキャッシュヒットし API 呼び出し 1 回のみを確認）
   - 検索は `pgvector` HNSW コサイン類似度（重み `1.2`）＋ キーワード `ILIKE` マッチ（重み `0.8`）の **RRF（k=60）**
4. **自動品質・漏洩評価ゲート（`eval_rag.py` / `eval_dataset.yaml`）**
   - 12 問の日本語ゴールデン質問（ADR・設計・監査・ライブマート横断）を毎回実行し、`rag.eval_runs` / `rag.eval_results` へ履歴保存
   - 全チャンクに対する漏洩再スキャン（`leakage_violations == 0` 必須）と `Hit@3 >= 80%` を合格ゲートとし、違反時は終了コード非 0 ＋ Zabbix Trapper アラート発報
   - **実測結果**：`Hit@1 = 91.67%`（11/12）・**`Hit@3 = 100.0%`**（12/12）・`Hit@5 = 100.0%`・**`MRR@5 = 0.9583`**・平均検索レイテンシ `376.71 ms`・**`leakage_violations = 0`**

### MCP 公開ツール・Grafana・Zabbix 連携

- **MCP ツール（`infra-rag-mcp`・Streamable HTTP JSON-RPC 2.0）**：
  - `search_home_infra_knowledge(query, top_k=5, source_type?)`：出典パス・文書状態・見出し・スコア付きで RRF ハイブリッド検索結果を返却
  - `get_infra_governance_snapshot()`：Step 1 `mart.*` から最新の日次 KPI・資産突合・ID/MFA ポスチャー・バックアップ履歴・重要脆弱性サマリを返却
  - OpenCode 側検証：`opencode mcp list` で `infra-rag connected` を確認し、`opencode run --agent batch` から両ツールを自動呼び出しして出典付き回答（UGREEN DXP4800 Plus 選定理由＋ライブ MFA カバレッジ `100.0%` 等）を取得できることを実機確認済み
- **Grafana Provisioning ダッシュボード**：`Infra RAG Governance & Retrieval Quality`（`uid: infra-rag-governance-v1`・9 パネル：インデックス文書/チャンク数・`Hit@1` / `Hit@3` / `MRR@5`・漏洩件数・評価トレンド・質問別ランキング・文書一覧）を `https://bi.home.example.com` へ自動配備
- **Zabbix Trapper 監視（計 9 アイテム・4 トリガー）**：Step 1 の 5 アイテムに加え、`infra.rag.status` / `infra.rag.chunks` / `infra.rag.hit_at_3` / `infra.rag.leakage` と 2 トリガー（同期・評価異常、および秘密情報漏洩検知または `Hit@3 < 80%`）を追加し、`processed: 9; failed: 0` を確認済み
- **Step 2 完了後の `ubuntu-desktop` 実測リソース**：全 20 コンテナ稼働中で空きメモリ available **`6.6 GiB / 11 GiB`**・SSD 空き **`156 GiB / 218 GiB`**（使用率 25%）、データ基盤 3 コンテナ合計消費メモリ **`225.8 MiB`**（`infra-dwh-postgres` `48.0 MiB` ＋ `infra-dwh-grafana` `145.3 MiB` ＋ `infra-rag-mcp` `32.5 MiB`）

## Step 3：ワークフロー自動化（n8n）＆定量 ROI 計測（構築済み）

[stacks/n8n/](../stacks/n8n/) に `n8nio/n8n:2.48.7` を導入し、Step 2 の `infra-rag-mcp` を活用した自動トリアージ／統制ダイジェストと、Step 1 の `infra_dwh` ＋ Grafana による自動化効果（ROI）の定量計測を接続した。

### コンポーネント・認証・DB 隔離

| 項目 | 構成・実測 |
| --- | --- |
| **コンテナ** | `n8n`（`n8nio/n8n:2.48.7`・非 root `1000:1000`・`cap_drop: [ALL]`・`mem_limit: 768m` / 実測 `386 MiB`・`N8N_BLOCK_ENV_ACCESS_IN_NODE=true`） |
| **HTTPS＆認証** | `https://n8n.home.example.com`（Caddy ＋ **`authentik-server` 内蔵 Embedded Outpost `forward_auth`** で MFA 保護・無認証時 `302` 確認済み。`forward_auth` バイパスは `/webhook/zabbix-triage`・`/webhook/governance-digest` の固定パスのみに限定し `X-Webhook-Token` Header Auth 必須・無認証時 `403` 確認済み） |
| **DB 隔離** | `infra-dwh-postgres` 内に専用 DB **`n8n_app`**・専用ロール `n8n_app`（`infra_dwh` / `grafana_app` への `CONNECT` 拒否確認済み）と、`infra-dwh-pipeline` 専用読み取りロール **`n8n_ro`**（`workflow_entity` / `execution_entity` のみ `SELECT` 可、`credentials_entity` / `execution_data` は `permission denied` 確認済み） |
| **バックアップ** | `stacks/backup/backup_phase1_db.py` に `n8n_app` を追加（`pg_dump` 745 KB 検証済み）。ワークフロー JSON は `stacks/n8n/workflows/` で Git 管理、API 鍵類は `N8N_ENCRYPTION_KEY` で DB 暗号化保存 |

### 配備済みワークフロー（`stacks/n8n/workflows/`）

1. **`WF01: Zabbix 障害トリアージ`（`wf01-zabbix-alert-rag-triage.json`）**
   - `POST /webhook/zabbix-triage`（`X-Webhook-Token` 必須）→ `infra-rag-mcp`（`search_home_infra_knowledge` ＋ `get_infra_governance_snapshot`）→ さくらの AI Engine（`preview/DeepSeek-V4.1-Flash-1017`）で原因推定・初動コマンド・出典設計書パスを要約 → Discord 通知（`dry_run: true` で非通知テスト可。想定削減工数 `15 分/回`）
2. **`WF02: 日次インフラ統制ダイジェスト`（`wf02-daily-infra-governance-digest.json`）**
   - 毎朝 `04:15 JST`（`04:00` バッチ直後）または `POST /webhook/governance-digest` → `infra-rag-mcp` ライブスナップショット取得 → 資産不整合・MFA 100% 未満・重大 CVE・RAG 精度低下/漏洩のいずれかを検知した時だけ Discord 通知（想定削減工数 `10 分/回`）

### 定量 ROI 計測パイプライン（`dlt` → `dbt` → Grafana）

- **`dlt` 6 系統目（`ingest.py`）**：`n8n_ro` 経由で `raw.n8n_workflows`・`raw.n8n_executions` を冪等取込
- **`dbt` シード＆マート**：`dbt/seeds/workflow_roi_assumptions.csv`（1 回あたり想定手動削減分数の正本）＋ `stg_n8n_executions` ＋ **`mart_automation_roi`** ＋ **`mart_automation_roi_daily`** を追加し、全体で **`dbt test` `PASS=21 / WARN=0 / FAIL=0`** を確認済み
- **Grafana Provisioning ダッシュボード**：`Infra Automation & ROI Analytics`（`uid: infra-automation-roi-v1`・7 パネル：稼働ワークフロー数・累計実行回数・成功率・推定削減工数・日次トレンド・ワークフロー別 ROI 一覧）を `https://bi.home.example.com` へ自動配備（全 3 ダッシュボード計 27 パネル稼働中）
- **Step 3 完了後の `ubuntu-desktop` 実測リソース**：全 **21 コンテナ**稼働中で空きメモリ available **`6.1 GiB / 11 GiB`**・SSD 空き **`154 GiB / 218 GiB`**（使用率 26%）
