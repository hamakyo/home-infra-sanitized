# home-infra-sanitized

自宅基盤（NAS・バックアップ・ネットワーク・常駐サービス・統制データ基盤）の設計書・決定記録（ADR）・構成図・IaC / Docker Compose スタックをまとめた公開リファレンス版リポジトリ。

> **Sanitized Public Edition**
> 本リポジトリは、実運用中の自宅インフラ構成を参考に設計・構成・運用手順を一般化した公開リファレンス版です。実環境の識別情報・秘密情報・個人情報の露出を避けるため、ドメイン・Tailscale IP・LAN サブネット・アカウント名・実機型番／シリアル・他プライベートリポジトリ名・定時実行時刻・実作業日時・実測スナップショット ID・ホスト監査ログはサンプル値へ置換または除外しています。
> 公開前には [scripts/check_sanitized.py](scripts/check_sanitized.py)（テキスト・HTML・SVG・PNG メタデータ検査）および GitHub Actions（`gitleaks` 全履歴スキャン）による自動検査と手動レビューを実施していますが、実環境の完全な匿名化を絶対的に保証するものではありません。

運用の約束事は [AGENTS.md](AGENTS.md)。サニタイズ漏れ防止の自動検査は [scripts/check_sanitized.py](scripts/check_sanitized.py) および GitHub Actions（`gitleaks` 併用）で実行する。

## 全体像

データの流れは「端末 → NAS → クラウド」の一方向に寄せる。

- 正本は GitHub の各データセット用別リポジトリ。NAS はそのローカルミラーを持つ
- macOS 端末の世代バックアップは Time Machine で NAS へ
- NAS にしかないデータ（写真、書類、メディアの原本）は `restic` でクライアント側暗号化して Backblaze B2 へ
- サーバー／NAS 上の Docker に監視・ID 基盤・CMDB・データ基盤・自動化ワークフロー・隔離 AI エージェントを常駐させる

## ドキュメント

- [自宅デバイスの構成（サンプル）](docs/devices.md) — 状態： 運用中（サニタイズ済みサンプル構成）
- [自宅ネットワークの構成（サンプル）](docs/network.md) — 状態： 運用中（二重 NAT ＋ Tailscale 閉域・架空サブネット構成）
- [自宅基盤の目標構想（Yamaha 3台体制＋バックアップ）](docs/target-architecture.md) — 状態： 設計（目標構想・未構築）
- [バックアップ体制の設計](docs/backup-design.md) — 状態： 構築済み（管理基盤 DB 群＋`.env` 一式＋ホスト設定＋暫定ファイルサーバー重要データの B2 日次退避＋Git 日次複製・Zabbix 監視・復元訓練済み／本番 NAS 連携は未構築）
- [障害復旧手順書（Disaster Recovery Runbook）](docs/disaster-recovery.md) — 状態： 構築済み（別 Ubuntu 24.04 マシンへの完全復旧手順・`restore_dr.py` 実機 `verify` 検証済み）
- [監視基盤の設計（Zabbix）](docs/monitoring-design.md) — 状態： 構築済み（Phase 1：ubuntu-desktop で稼働中）
- [デバイス管理の設計（Fleet・NetBox・Ansible）](docs/device-management-design.md) — 状態： 構築済み（Phase 1：NetBox v4.7・Fleet Free v4.95.0・Jamf Now 無料枠・Ansible ホスト構成管理 稼働中）
- [ID・秘密情報管理の設計（Authentik + 1Password）](docs/identity-design.md) — 状態： 構築済み（Phase 1：Authentik 2026.8.5 稼働中・4 基盤 SSO＋MFA 強制・Break-glass 疎通完了）
- [隔離 AI エージェント実行基盤の設計（OpenCode）](docs/agent-runtime-design.md) — 状態： 構築済み（Step 1〜3 完了：OpenCode 1.19.6 隔離コンテナ・対話利用・定時ジョブ 3 本稼働中）
- [自宅インフラ 統制・可視化データ基盤の設計（Infra Data & AI Hub）](docs/data-platform-design.md) — 状態： 構築済み（Step 1〜Step 3 完了：`https://bi.home.example.com`・`https://n8n.home.example.com`）
- [暫定ファイルサーバーの構成（ubuntu-desktop + USB SSD）](docs/fileserver.md) — 状態： 運用中（外付け USB SSD 480GB・Samba SMB3・`/mnt/files/important` B2 日次退避稼働中）
- [データの置き場所の方針（YAML から DB へ）](docs/database-policy.md) — 状態： 方針（移行は未実施）

## 決定の記録（ADR）

採用した案だけでなく、落とした案とその理由を連番で残している。

- [ADR-0012 Step 3 のワークフロー自動化基盤に n8n（Authentik Forward Auth ＋ ROI 計測連携）を選んだ](docs/decisions/0012-workflow-automation-n8n.md)
- [ADR-0011 Step 2 のガバナンス付き RAG 基盤に既存 pgvector・さくらの AI Engine 無料枠・Read-Only MCP Gateway を選んだ](docs/decisions/0011-rag-pipeline-pgvector-sakura-mcp.md)
- [ADR-0010 自宅インフラ 統制・可視化データ基盤の Step 1 スタックに PostgreSQL (pgvector)・dlt・dbt-core・Grafana OSS を選んだ](docs/decisions/0010-data-platform-infra-stack.md)
- [ADR-0009 Apple MDM は Apple Business なしの Jamf Now 無料枠で最小運用に留める](docs/decisions/0009-apple-mdm-jamf-now-scope.md)
- [ADR-0008 隔離された常駐 AI エージェントのランタイムに opencode serve / opencode run を選んだ](docs/decisions/0008-agent-runtime-opencode.md)
- [ADR-0007 自宅基盤のリバースプロキシと HTTPS 終端に Caddy と *.home.example.com を選んだ](docs/decisions/0007-reverse-proxy-caddy-wildcard-tls.md)
- [ADR-0006 自宅基盤の ID・秘密情報管理に Authentik と 1Password を選んだ](docs/decisions/0006-identity-authentik-1password.md)
- [ADR-0005 自宅基盤のデバイス管理に Fleet（FleetDM）・NetBox・Ansible を選んだ](docs/decisions/0005-device-management-fleet-netbox.md)
- [ADR-0004 自宅基盤の監視ツールに Zabbix を選んだ](docs/decisions/0004-monitoring-zabbix.md)
- [ADR-0003 自宅ネットワーク刷新の候補に Yamaha（RTX830・SWX2210-8G・WLX222）を選んだ](docs/decisions/0003-network-yamaha-stack.md)
- [ADR-0002 NAS に UGREEN NASync DXP4800 Plus を選んだ](docs/decisions/0002-nas-ugreen-dxp4800-plus.md)
- [ADR-0001 クラウド側の置き先に Backblaze B2 を選んだ](docs/decisions/0001-cloud-backup-backblaze-b2.md)

## 構成サマリ

- **端末・ネットワーク**：PC 3 台・iOS 端末・IoT 機器・上流ルーター配下の二重 NAT 構成＋Tailscale 閉域メッシュ（[docs/devices.md](docs/devices.md)・[docs/network.md](docs/network.md)）
- **Phase 1 管理基盤（Docker）**：**Caddy**（`*.home.example.com` Let's Encrypt ワイルドカード証明書・Tailscale 限定）、**Zabbix 8.0 LTS**（Discord 通知・SAML SSO）、**NetBox v4.7**（CMDB/IPAM・OIDC SSO）、**Fleet Free v4.95.0**（PC 3 台登録・SAML SSO）、**Authentik 2026.8.5**（全 4 基盤 SSO＋Passkey/TOTP MFA 強制・Break-glass 疎通完了）、**OpenCode 1.19.6**（5 層隔離コンテナ・対話利用＆定時ジョブ 3 本）
- **Infra 統制・可視化データ基盤＆自動化（Step 1〜Step 3）**：`infra-dwh-postgres`（`pgvector/pgvector:0.8.7-pg17-trixie`）・`infra-dwh-grafana`（`grafana/grafana-oss:12.4.3`・Authentik OIDC SSO・Provisioning 3 ダッシュボード計 27 パネル）・Read-Only RAG MCP サーバー `infra-rag-mcp`・`n8n 2.48.7`（Authentik Forward Auth・固定パス限定 Webhook＋Header Auth・RAG トリアージ＆日次統制ダイジェスト・ROI 定量計測）。毎朝 `04:00 JST`（サンプル設定）に非定常駐 `infra-dwh-pipeline`（6 系統 `dlt`＋`dbt-core`＋RAG 増分同期・12 問評価）を実行
- **バックアップ＆DR 体制**：管理基盤 DB 群＋全スタック `.env` 一式＋ホスト設定＋暫定ファイルサーバー重要データを `restic` で Backblaze B2 へ暗号化日次退避（Zabbix Trapper 監視・復元訓練済み）＋ GitHub → GitLab 日次複製・ref 検証＋[ansible/](ansible/)（NetBox 動的インベントリ＆DR 静的インベントリ）＋[stacks/backup/restore_dr.py](stacks/backup/restore_dr.py)
- **Apple MDM 最小運用**：Jamf Now 無料枠（Apple Business なし・OS 別ブループリント分離・FileVault 個人復旧キー退避）
- **導入予定（未購入）**：本番 NAS（UGREEN NASync DXP4800 Plus + WD Red Plus 8TB ×4）、ネットワーク刷新候補（Yamaha RTX830・SWX2210-8G・WLX222）

## スタック・ジョブ定義

- [ansible/](ansible/) — Ubuntu ホスト構築・ハードニング・Docker・Zabbix Agent・restic/cron・Samba USB 安定化・全 8 スタック起動順・NetBox v4.7 動的インベントリ連携の Ansible IaC
- [stacks/caddy/](stacks/caddy/) — リバースプロキシ・`*.home.example.com` ワイルドカード TLS（Cloudflare DNS-01）
- [stacks/zabbix/](stacks/zabbix/) — Zabbix 8.0 LTS + PostgreSQL 16 監視基盤
- [stacks/netbox/](stacks/netbox/) — NetBox v4.7 + PostgreSQL 18 + Valkey 9.1 機器・IP 台帳
- [stacks/fleet/](stacks/fleet/) — Fleet Free v4.95.0 + MySQL 8.4 + Redis 7 端末管理・CVE 検知
- [stacks/authentik/](stacks/authentik/) — Authentik 2026.8.5 + PostgreSQL 16 IdP・SSO 基盤
- [stacks/opencode/](stacks/opencode/) — OpenCode 1.19.6 隔離 AI エージェント実行基盤（非 root・`read_only`・`opencode-proxy`＋`rag-mcp-net`・Read-Only RAG MCP 連携）
- [stacks/backup/](stacks/backup/) — 管理基盤 DB 群・`.env` 一式・ホスト設定 ＆ 暫定ファイルサーバー重要データ → `restic` → Backblaze B2 日次暗号化バックアップ・DR 自動復元（`restore_dr.py`）・Zabbix Trapper 連携
- [stacks/repolica/](stacks/repolica/) — GitHub → GitLab 日次 Git リポジトリ複製・ref 検証・Zabbix Trapper 連携
- [stacks/data-platform/](stacks/data-platform/) — Infra 統制・可視化データ基盤 Step 1〜3（PostgreSQL 17 `pgvector`・6 系統 `dlt`＋`dbt-core`・ガバナンス付き RAG 日次バッチ・Grafana OSS `12.4.3`・Read-Only MCP Gateway `infra-rag-mcp`・Zabbix Trapper 連携）
- [stacks/n8n/](stacks/n8n/) — `n8n 2.48.7` ワークフロー自動化基盤（Authentik Forward Auth・専用 DB `n8n_app`・RAG トリアージ＆日次統制ダイジェスト）
- [stacks/fileserver/](stacks/fileserver/) — `ubuntu-desktop` 暫定ファイルサーバー（外付け USB SSD `ext4` マウント・UAS 無効化＆udev 安定化・Samba SMB3・Avahi・UFW 導入スクリプト）
- [scripts/check_sanitized.py](scripts/check_sanitized.py) — 個人情報・実環境識別子・実作業日時・PNG メタデータ・秘密情報の混入を検出するサニタイズ検査スクリプト
