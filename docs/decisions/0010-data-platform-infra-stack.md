# 自宅インフラ 統制・可視化データ基盤の Step 1 スタックに PostgreSQL (pgvector)・dlt・dbt-core・Grafana OSS を選んだ

状態： 構築済み（Step 1 完了：`ubuntu-desktop` で稼働中）

## 決定

`ubuntu-desktop` 上に構築する自宅インフラ 統制・可視化データ基盤（Step 1）の技術スタックとして、次の組み合わせを隔離 Docker 構成で採用する。

- **データウェアハウス / 格納先**：**PostgreSQL 17（`pgvector/pgvector:0.8.7-pg17-trixie`）**
  - 既存の `docs/database-policy.md`（本線は PostgreSQL）と整合させつつ、将来の Step 2（ガバナンス付き RAG・ベクトル検索）を見据えて最初から `pgvector` 同梱イメージを採用する
- **データ収集（EL）**：**Python 3.12 ＋ `dlt`（`dlt[postgres]`）**
  - Authentik・NetBox・Fleet Free・Zabbix・`restic` バックアップ実行ログの 5 系統から日次スナップショットおよびイベントを冪等に取り込む
- **データ変換・統制テスト（T）**：**`dbt-core`（`dbt-postgres`）**
  - Medallion 構造（`raw` → `stg` → `mart`）の SQL 変換と、ツール間資産突合・MFA・重要 CVE・バックアップ鮮度の自動検証（`dbt test`）を担う
  - 収集と変換は常駐させず、ワンショットのバッチコンテナをホスト側 `cron`（毎日 `04:00 JST`）から起動して待機メモリを `0 MiB` に抑える
- **可視化・BI（ダッシュボード）**：**Grafana OSS `12.4.3`**（`grafana/grafana-oss:12.4.3`）
  - 既存の Caddy（`https://bi.home.example.com`・Tailscale 閉域）と **Authentik OIDC SSO**（`[auth.generic_oauth]`）に無料枠のまま直接統合し、データソース定義とダッシュボード JSON を Git（Provisioning as Code）で管理する

詳細設計は [自宅インフラ 統制・可視化データ基盤の設計](../data-platform-design.md) を参照。

## 要件

- `ubuntu-desktop` の実測空き資源（空きメモリ `7.1 GiB`・SSD 空き `159 GiB`・既存 17 コンテナ稼働中）の範囲内で、追加常駐メモリを **1.5 GiB 以内**（理想は `1 GiB` 前後）に収めること
- 既存の管理基盤（Authentik・NetBox・Fleet Free・Zabbix・`restic` バックアップ）とネットワーク・DB・秘密情報を分離し、データ基盤側の障害や負荷が Phase 1 稼働サービスへ波及しないこと
- 追加の認証方式や Outpost コンテナを増やさず、Authentik の **OIDC SSO** で既存 3 基盤と同等のログイン・MFA 統制に乗せられること
- 構成・変換 SQL・データ品質テスト・ダッシュボード定義をすべて Git で正本管理でき、将来の NAS（`DXP4800 Plus`）移設や Step 2（RAG）・Step 3（ワークフロー自動化）へ無理なく拡張できること

## 理由

- **OSS 無料枠でネイティブ OIDC SSO が完結する**：Grafana OSS は `[auth.generic_oauth]` で Authentik OIDC SSO にそのまま対応できるため、追加の LDAP Outpost や有料ライセンスなしで全基盤の ID 統制を統一できる
- **省メモリかつ宣言的なダッシュボード運用**：Grafana OSS は Go 製で待機メモリが約 `150〜300 MiB` と軽く、Provisioning 機能によりデータソース（最小権限の `bi_reader`）と統制ダッシュボードを Git 管理・自動再現できる
- **`pgvector` 入り PostgreSQL を中心ハブに据える将来性**：Step 1 の構造化データマートと、Step 2 で予定しているドキュメント・運用履歴の RAG（`pgvector`）を同じ DB エンジンで扱えるため、DB の乱立と移行手戻りを防げる。`pg_dump` で既存の `restic` → Backblaze B2 バックアップ経路にもそのまま乗せられる
- **バッチ非定常駐でメモリを食わない**：`dlt` と `dbt-core` をワンショット実行コンテナ＋ホスト `cron`＋Zabbix Trapper 監視にすることで、常駐オーケストレーターのオーバーヘッドを避けつつ、既存のバックアップ・OpenCode 定時ジョブと同じ運用モデルに統一できる

## 落とした案

- **Metabase OSS（BI 層）**：非エンジニアでも扱いやすいアドホック分析 UI は魅力的だが、OSS 版ではネイティブ OIDC / SAML SSO が非対応（有料プラン限定）であり、Authentik 連携には別コンテナの LDAP Outpost 追加（かつブラウザリダイレクト SSO 不可）が必要になる。加えて JVM 製で常駐 `1.0〜1.5 GiB` を消費し、2026 年にも認証前 SQLi 等の重大 CVE（CVE-2026-72898 / CVE-2026-72900）が発生した経緯があるため、今回の一次選定からは外した
- **DuckDB 単体（DWH 層）**：単一ファイルで分析クエリが高速だが、複数クライアントからの同時読み書き・ロール単位の権限分離（`etl_writer` / `bi_reader`）・Step 2 の `pgvector` 連携・`docs/database-policy.md` の PostgreSQL 本線との整合性では PostgreSQL に分があるため見送った（将来、大容量 Parquet のローカル分析が必要になった場合の補助エンジンとして残す）
- **Apache Airflow / Dagster / Kestra（オーケストレーション層）**：依存関係管理や Web UI は充実しているが、1 日 1 回の 5 系統収集＋`dbt` 実行に対して常駐メモリ消費（500 MiB〜数 GiB）と保守コストが過剰なため、Step 1 ではホスト `cron`＋排他ロック＋Zabbix Trapper に留める
- **Apache Superset（BI 層）**：Python / Flask + Celery + Redis 等の構成が重く、運用・アップグレード負荷とメモリ消費が `ubuntu-desktop` の余力に対して大きいため見送った
- **Spark / ClickHouse / Kafka 等の分散・大容量スタック**：対象データ量が数千〜数万行規模の自宅インフラ 統制データであり、12 GB メモリの単一ホストで運用する意義がないため対象外とした

## 注意

- **DB ロールの最小権限分離を徹底する**：Grafana へ渡す DB ユーザー（`bi_reader`）は `mart` スキーマの `SELECT` のみに限定し、`raw` スキーマや書き込み権限を与えない
- **Grafana の Break-glass 管理者と秘密情報管理**：Authentik 障害時に備えてローカル管理者を 1 アカウント保持し、パスワード・API トークン・OIDC シークレットはホスト上の `chmod 600 .env`（git 管理外）のみで管理する
- **既存バックアップとの整合**：構築完了後、`infra-dwh-postgres` を `stacks/backup/backup_phase1_db.py` の退避対象へ組み込むか、再構築可能データと永続設定（Grafana DB）の扱いを明文化する
- **Phase 2（NAS 導入後）の移設**：将来 `DXP4800 Plus` へ Phase 1 基盤を移設する際、`stacks/data-platform/` も `pg_dump` と Compose / Provisioning 定義ごとそのまま移行できる設計を維持する
