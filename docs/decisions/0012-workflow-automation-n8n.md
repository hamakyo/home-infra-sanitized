# Step 3 のワークフロー自動化基盤に n8n（Authentik Forward Auth ＋ ROI 計測連携）を選んだ

状態： 構築済み（Step 3 完了：`ubuntu-desktop` で稼働中）

## 決定

自宅インフラデータ基盤（`stacks/data-platform/`）の Step 3（イベント駆動ワークフロー自動化＆定量効果測定）として、**[stacks/n8n/](../../stacks/n8n/) に `n8nio/n8n:2.48.7` をセルフホスト導入**し、次の構成で統合する。

- **認証（SSO 非対応の補完）**：Community Edition はネイティブ OIDC/SAML 非対応のため、既存の **`authentik-server` 内蔵 Embedded Outpost** と Caddy `forward_auth` を組み合わせて `https://n8n.home.example.com` 前段へ Authentik MFA ゲートを配置する（追加 Outpost コンテナ不要。`/rest/push` WebSocket 用に `forward_auth` 側で `Connection`/`Upgrade` ヘッダー除去＋`flush_interval -1` を適用）
- **Webhook 保護**：機械呼び出し向けに `forward_auth` をバイパスするのは明示許可した稼働中ワークフローの固定パス（`/webhook/zabbix-triage`・`/webhook/governance-digest`）のみに限定し（`/webhook-test/*` は対象外）、各ワークフローで **`X-Webhook-Token` Header Auth**（暗号化クレデンシャル）を必須化し無認証時は `403` で拒否する
- **DB 分離＆暗号化クレデンシャル**：既存の `infra-dwh-postgres`（PostgreSQL 17）内に専用 DB **`n8n_app`** と専用ロール **`n8n_app`** を作成し、`infra_dwh` / `grafana_app` への `CONNECT` を明示遮断する。さらに `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` を有効化し、API キー類はすべて `N8N_ENCRYPTION_KEY` で暗号化した n8n クレデンシャルのみ経由させる
- **Step 1 / Step 2 との統合（RAG 連携＆ROI 計測）**：
  - `n8n` は `rag-mcp-net` 経由で Read-Only MCP サーバー **`infra-rag-mcp`** を呼び出し、設計書 RAG 検索とライブ統制スナップショットを活用する
  - 逆に `infra-dwh-pipeline` からは最小権限ロール **`n8n_ro`**（`workflow_entity`・`execution_entity` の `SELECT` のみ許可、`credentials_entity` / `execution_data` は `permission denied`）で実行履歴だけを日次取込し、`dbt`（`mart_automation_roi` / `mart_automation_roi_daily`）と Grafana（`Infra Automation & ROI Analytics`）で自動化の回数・成功率・推定削減工数（分/時間）を定量可視化する
- **バックアップ連携**：`n8n_app` を `stacks/backup/backup_phase1_db.py`（`restic` → Backblaze B2 日次退避）へ組み込み、ワークフロー定義 JSON は `stacks/n8n/workflows/` で Git 管理する

詳細設計は [自宅インフラ 統制・可視化データ基盤の設計](../data-platform-design.md) を参照。

## 要件

- **実務に直結するワークフロー運用知見**：Webhook 起点の障害トリアージ・RAG 連携・定時ダイジェスト・将来の人間承認（Human-in-the-Loop）までを標準ノードで組めること
- **既存基盤のセキュリティ境界を崩さない**：`n8n` が他基盤の DB（`infra_dwh` / Authentik / NetBox / Fleet / Zabbix）へ直接触れず、外部公開も Tailscale 閉域＋Authentik Forward Auth＋Webhook Header Auth で多層防御すること
- **定量効果測定（ROI 可視化）**：ワークフローの実行回数・成功率・推定削減工数を既存 `infra_dwh` ＋ Grafana へ自動連携できること
- **ホスト余力の範囲内**：`ubuntu-desktop`（12 GB RAM）で `mem_limit: 768m` 以内に収まり、全体空きメモリに十分な余裕を残すこと（実測：`n8n` `386 MiB`・全 21 コンテナ稼働中でホスト空きメモリ `6.1 GiB`）

## 理由

- **実務採用率と表現力のバランスが最も高い**：Webhook・HTTP API・AI（OpenAI 互換 / MCP）・Discord・GitHub・承認フローを GUI で見通しよく構成でき、運用パターンがそのまま現場の業務自動化・運用設計に活きる
- **Authentik Embedded Outpost で無料枠 SSO 欠点を綺麗に補えた**：別コンテナを増やさず既存の `authentik-server:9000` だけで Forward Auth を実現でき、Passkey / TOTP MFA の統制下に置けた
- **実行履歴テーブルの分離抽出が容易**：PostgreSQL バックエンド（`n8n_app`）を使うことで、機密テーブル（`credentials_entity` / `execution_data`）を完全遮断しつつメタデータだけを `dlt` → `dbt` → Grafana へ流す監査・ROI 基盤が最小実装で完結した

## 落とした案

- **Python（Starlette Webhook＋既存 cron）のみで自作する案**：最軽量で完全コード管理できるが、分岐・リトライ・視覚的な実行トレース・将来の承認ステップや外部 SaaS 連携を都度コード実装する手間が大きく、実務のローコード自動化ツール運用経験にも繋がりにくいため見送った
- **Windmill**：Rust/Go ベースで軽量かつ Git 同期・コード中心設計が優秀だが、実務での普及度・既存ノードエコシステムでは `n8n` が上回るため今回は `n8n` を採用した
- **Activepieces**：MIT ライセンスで軽量だが、インフラ運用・Webhook 高度制御・AI 連携の成熟度で `n8n` を優先した

## 注意

- **`N8N_ENCRYPTION_KEY` の保全**：`stacks/n8n/.env`（`chmod 600`・git 管理外）の `N8N_ENCRYPTION_KEY` を紛失すると、B2 バックアップから `n8n_app` を復元しても暗号化クレデンシャルを復号できない。パスワードマネージャーへ必ず控えを残すこと
- **初回オーナーアカウント作成**：`https://n8n.home.example.com` を Authentik Forward Auth 経由で初めて開いた際、n8n 側のローカルオーナーアカウントを 1 回作成してパスワードマネージャーへ控える（UI 操作・API キー発行用。ワークフロー自体は CLI インポート＆公開済みのため初回ログイン前でも稼働する）
- **ワークフロー JSON の Git 同期**：UI 上でワークフローを編集した場合は `docker exec n8n n8n export:workflow --id=<id>` で `stacks/n8n/workflows/` へ書き戻し、秘密情報を含まないことを確認して Git 管理する
