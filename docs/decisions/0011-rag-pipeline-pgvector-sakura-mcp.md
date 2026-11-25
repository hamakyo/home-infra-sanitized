# Step 2 のガバナンス付き RAG 基盤に既存 pgvector・さくらの AI Engine 無料枠・Read-Only MCP Gateway を選んだ

状態： 構築済み（Step 2 完了：`ubuntu-desktop` で稼働中）

## 決定

Step 1 の Infra 統制・可視化データ基盤（`stacks/data-platform/`）へ追加する Step 2（ガバナンス付き RAG パイプライン＆エージェント連携）として、次の構成を採用する。

- **ベクトル格納＆ハイブリッド検索**：既存の **`infra-dwh-postgres`（PostgreSQL 17 / `pgvector 0.8.7`）** 内に `rag` スキーマを新設し、HNSW コサイン類似度検索とキーワード検索の **Reciprocal Rank Fusion（RRF）** を実装する
- **埋め込みモデル**：**さくらの AI Engine 無償プランの `multilingual-e5-large`（1024 次元・国内完結 ZDR・月 10,000 リクエスト無料・自動課金なし）** を採用する
- **同期・ガバナンス基盤**：非定常駐の `infra-dwh-pipeline` へ Step 2 ステージを追加し、**許可リスト対象（`home-infra` の Markdown と Step 1 `mart.*` ライブサマリのみ）→ 決定論的 PII / 秘密情報マスクゲート → SHA-256 増分キャッシュ同期 → 12 問ゴールデン評価（`Hit@3` / `MRR@5` / ゼロ漏洩ゲート）** を毎朝 `04:00 JST` に自動実行する
- **OpenCode との接続方式**：隔離コンテナ `opencode` へ PostgreSQL 接続情報を直接渡さず、専用の **Read-Only Streamable HTTP MCP サーバー（`infra-rag-mcp`・非 root UID `10001`・`read_only: true`・`cap_drop: [ALL]`・Bearer 認証）** を前段に置き、新規分離ネットワーク **`rag-mcp-net`** 経由でのみ接続する
- **可視化・監視**：Grafana Provisioning に **`Infra RAG Governance & Retrieval Quality`（9 パネル）** を追加し、Zabbix Trapper へ `infra.rag.*` の 4 アイテム・2 トリガーを追加する

詳細設計は [自宅インフラ 統制・可視化データ基盤の設計](../data-platform-design.md) および [隔離 AI エージェント実行基盤の設計（OpenCode）](../agent-runtime-design.md) を参照。

## 要件

- **追加課金ゼロ（0 円）かつ国内完結 ZDR**：無料枠超過時に自動課金が発生せず、設計書・運用メタデータが国外へ学習転用されないこと
- **OpenCode の 5 層隔離を維持**：`opencode` から `infra-dwh-postgres` および Phase 1 各 DB への到達経路を一切作らず、参照先も `rag.*` / `mart.*` の `SELECT` のみに厳格限定すること
- **データガバナンスと定量品質保証**：個人情報を含む別リポジトリ や `.env` を絶対に取り込まない許可リスト方式とし、万一の秘密情報・PII 混入を同期前マスク＋評価時全件スキャンで二重遮断し、検索品質（`Hit@3` / `MRR@5`）を日次で自動記録・監視できること
- **省リソース**：`ubuntu-desktop`（12 GB RAM）上の追加常駐メモリを最小限（実測 `32.5 MiB`）に抑えること

## 理由

- **既存 `pgvector` の活用で DB とバックアップ経路を増やさない**：Step 1 で `pgvector` 同梱の PostgreSQL 17 を採用した投資をそのまま活かせ、`rag.*` も既存の `restic` → Backblaze B2 日次退避に自動包含される
- **さくらの AI Engine `multilingual-e5-large` が日本語・コスト・ZDR の 3 条件を満たす**：日本語検索精度が高く（12 問ゴールデン評価で初回 `Hit@1 91.67%`・`Hit@3 100.0%`・`MRR@5 0.9583`）、SHA-256 増分キャッシュにより定常日の埋め込み消費を月数百リクエスト以下（無料枠 10,000 回の数%）に抑えられる
- **Read-Only MCP Gateway による最小権限境界**：`infra-rag-mcp` だけが `infra-dwh-net` 上の Postgres へ最小権限ロール `rag_reader`（`default_transaction_read_only=on`、`raw` / `stg` 権限なし）で接続し、`opencode` は `rag-mcp-net` 上の Bearer 保護された 2 つの読み取り専用ツール（`search_home_infra_knowledge` / `get_infra_governance_snapshot`）しか呼べない

## 落とした案

- **さくらの AI Engine マネージド RAG ストレージ**：100 チャンクあたり 3 円の従量課金が発生し、チャンク構造や RRF ハイブリッド検索・評価ログの自前統制が効きにくいため不採用とした（無料の Embeddings API のみを使い、格納と検索は自前 `pgvector` で完結させる）
- **ローカル CPU / PyTorch（`sentence-transformers` 等）による埋め込み生成**：モデル重量と推論メモリ（1〜2 GiB 規模）が大きく、`ubuntu-desktop`（12 GB RAM・GPU なし）でのコンテナ肥大化と夜間バッチ遅延を招くため不採用とした
- **OpenCode へ直接 PostgreSQL 接続（`psql` / Postgres MCP）を許可する案**：`opencode` を `infra-dwh-net` へ接続すると既存の 5 層隔離境界が崩れ、SQL 発行の自由度が高すぎるため不採用とした
- **専用ベクトル DB（Qdrant / Milvus / Chroma 等）の追加**：数百〜数千チャンク規模の自宅インフラ 知識ベースに対して常駐コンテナと運用・バックアップ対象を増やす利点がないため不採用とした

## 注意

- **`multilingual-e5-large` の 512 トークン上限**：日本語・Markdown 表はトークン密度が高いため、`sync_rag.py` は見出し単位＋最大 500 文字（行単位分割）でチャンク化し、`search_lib.py` 側でも 650 文字の安全上限を設けている
- **許可リストの厳守**：取り込み対象は `example-user/home-infra-sanitized` の `README.md`・`docs/*.md`・`docs/decisions/*.md` と Step 1 `mart.*` 集計カードのみに固定する。個人情報を含む別リポジトリ や `.env`、秘密情報を含むファイルは絶対に追加しない
- **トークン共有の管理**：`RAG_MCP_TOKEN` はホスト上の `stacks/data-platform/.env` と `stacks/opencode/.env`（ともに `chmod 600`・git 管理外）でのみ管理し、ローテーション時は両ファイルを同時更新する
