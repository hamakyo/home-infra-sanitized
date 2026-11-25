# 監視基盤の設計（Zabbix）

状態： 構築済み（Phase 1：`ubuntu-desktop` で Zabbix 8.0 LTS + PostgreSQL 16・Discord 通知・Authentik SAML SSO 稼働中）

自宅基盤（ネットワーク機器・端末・NAS・バックアップジョブ・外部環境）の常時監視設計。ツール選定の経緯は [自宅基盤の監視ツールに Zabbix を選んだ](decisions/0004-monitoring-zabbix.md) を参照。

※ 現況（稼働中）は [自宅デバイスの現況](devices.md) と [自宅ネットワークの現況](network.md) を参照。設計と現況を混同しないこと。

## 構成図

![監視基盤の構成（Phase 1 稼働中）：既設ルーター・将来 Yamaha・スマート電球（IoT） ×3 を ICMP / 将来 SNMP で、PC 3台・将来 NAS と restic/同期ジョブを Zabbix Agent 2 / ICMP で、外部 VM（remote-vm-01） を Tailscale 経由で ubuntu-desktop 上の Zabbix 8.0 LTS へ集約。Zabbix Web（Caddy TLS / SAML SSO）と PostgreSQL 16 を Docker 上で稼働させ、警告以上を Discord Webhook へ通知、将来 pg_dump を restic で Backblaze B2 へ退避](diagrams/monitoring.png)

- 自己完結 HTML：[diagrams/monitoring.html](diagrams/monitoring.html)（`caru-ini/stack-diagram` で生成）

## 構成方針

- **コンポーネント**：`Zabbix Server`、`Zabbix Web`、`Zabbix Agent 2`、`PostgreSQL` を Docker Compose で管理する
- **DB 方針との整合**：[データの置き場所の方針](database-policy.md) に沿ってバックエンド DB には PostgreSQL を採用し、`pg_dump` による論理バックアップの出口を確保する
- **構成管理と秘密情報の分離**：Docker Compose 定義・テンプレート設定・運用手順は git で管理し、Web 管理パスワード・PostgreSQL パスワード・SNMP コミュニティ名 / 認証情報・通知用 Webhook URL は git へ書かず保管場所のみ記録する

## 監視対象と収集方式

| 対象 | 区分 | 収集方式 | 主に見る項目 |
| --- | --- | --- | --- |
| Yamaha `RTX830` / `SWX2210-8G` / `WLX222` | 将来導入 | SNMP + syslog（必要に応じて） | 死活、インターフェーストラフィック・リンクダウン、CPU / メモリ、稼働時間 |
| 既設ホームルーター | 現行（暫定） | ICMP Ping | 死活・応答遅延（Yamaha 導入までのつなぎ） |
| `workstation-01`（Windows）/ `macOS ノート PC`（macOS）/ ローカル Linux デスクトップ（Ubuntu Desktop LTS） | 現行 | Zabbix Agent 2 | 死活、CPU / メモリ / ディスク空き容量、プロセス・サービス状態 |
| `UGREEN NASync DXP4800 Plus` | 将来導入 | Zabbix Agent 2 | OS リソース、Docker コンテナ稼働、RAID 状態、HDD SMART 異常 |
| `スマート電球（IoT）` ×3 | 現行 | ICMP Ping | ネットワーク到達・死活 |
| `restic` 日次バックアップ / GitHub nightly pull | 現行（Phase 1 DB 稼働中）／将来（NAS） | Zabbix Trapper / Sender | ジョブ終了コード、最終成功時刻（28h 未実行検知）、所要時間、ダンプ容量 |
| `remote-vm-01`（外部 VM）（`100.64.0.20`） | 現行（外部） | Tailscale 経由（Agent 2 / ICMP） | 死活、リソース、別リポジトリ（外部 VM バックアップ） 稼働状況 |

## 配置と段階導入

NAS や Yamaha 機器の購入を待たず、手元の環境から段階的に立ち上げる。

1. **Phase 1（先行検証：ローカル Linux デスクトップ）— 稼働中**
   - `ubuntu-desktop` 上の Docker Compose（[stacks/zabbix/](../stacks/zabbix/)）で **Zabbix 8.0 LTS + PostgreSQL 16** を立ち上げ、Caddy（`https://zabbix.home.example.com`、Tailscale 閉域）配下で公開済み。Authentik との **SAML 2.0 SSO 連携**まで完了（ローカル `Admin` を Break-glass として保持）
   - `ubuntu-desktop` 本体にネイティブ `zabbix-agent2` を導入し、**Linux OS・Docker 全コンテナ・240 GB SSD の SMART** を監視中
   - `既設ホームルーター`（`192.168.50.1`）・`macOS ノート PC`（`100.64.0.30`）・`workstation-01`（`100.64.0.40`）を ICMP Ping で監視中（クライアント PC の詳細棚卸し・監査は稼働中の Fleet Free に委ねる方針）
   - アラート通知は **Discord（Webhook）** を有効化（深刻度：**警告以上**、テスト送信およびトリガーアクション有効化を確認済み）。スリープ・電源断による誤報を避けるため、クライアント端末グループ `Home/PC`（`workstation-01`・`macOS ノート PC`）は Discord 通知の対象外とし、24 時間稼働の `Home/Server`・`Home/Network`・`Zabbix servers` のみを通知対象に絞っている
   - **Phase 1 DB バックアップ監視（稼働中）**：[stacks/zabbix/configure_backup_monitoring.py](../stacks/zabbix/configure_backup_monitoring.py) で `ubuntu-desktop`（`Home/Server`）に Zabbix Trapper 5アイテム（`backup.phase1_db.status` / `last_success` / `duration_sec` / `dump_bytes` / `message`）と 2トリガー（失敗＝軽度の障害、28時間未成功＝警告）を登録済み。[stacks/backup/backup_phase1_db.py](../stacks/backup/backup_phase1_db.py) から `zabbix_sender` 経由で毎回送信し、異常時は Discord へ通知する
   - 残り：`スマート電球（IoT）` ×3（ルーター側で IP 予約後に ICMP 追加）、`remote-vm-01`（外部 VM） の監視追加
2. **Phase 2（本番集約：NAS 導入後）**
   - `DXP4800 Plus` 購入後、Zabbix コンテナ群を NAS 上の Docker へ移設・集約する
   - NAS 自身の RAID / SMART / Docker 監視、および `restic`・GitHub nightly pull のジョブ成否通知（Zabbix Trapper）を組み込む
   - PostgreSQL の `pg_dump` を日次で取り、`restic` → Backblaze B2 のバックアップ対象へ加える
3. **Phase 3（ネットワーク監視拡張：Yamaha 3台体制導入後）**
   - `RTX830`・`SWX2210-8G`・`WLX222` の導入に合わせて SNMP 監視を有効化し、ポートトラフィック・リンクダウン・VLAN 稼働を監視する

## 守り・バックアップ・相互監視

- **設定と履歴の退避**：PostgreSQL を `pg_dump` で論理ダンプし、NAS 上の他データと一緒に `restic` でクライアント側暗号化して Backblaze B2 へ退避する（[バックアップ体制の設計](backup-design.md) と一体運用）
- **監視基盤自身の停止検知**：Zabbix を載せるホスト（Phase 1 の Ubuntu、Phase 2 の NAS）自体が停止すると Zabbix からのアラートも届かない。別拠点である `remote-vm-01`（外部 VM） 等からの軽量な外形・死活チェックによる相互監視を将来検討する
- **秘密情報の扱い**：パスワード・SNMP 認証情報・Discord Webhook URL はリポジトリへコミットせず、当面はホストの `chmod 600 .env` や Zabbix DB 内で管理する（1Password 導入は一旦保留。詳細は [ID・秘密情報管理の設計](identity-design.md)）

## 未決事項

- **SNMP バージョン**：Yamaha 導入時に SNMPv2c（LAN 内部限定）とするか SNMPv3（認証・暗号化あり）とするか
- **データ保持期間**：History / Trends の保持日数（PostgreSQL 容量と学習目的のバランスを見て調整）
