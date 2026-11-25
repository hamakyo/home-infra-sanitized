# デバイス管理の設計（Fleet・NetBox・Ansible）

状態： 構築済み（Phase 1 Step 1A〜1D 完了：NetBox v4.7・Fleet Free v4.95.0・Jamf Now 無料枠・Ansible ホスト構成管理 稼働中／NetBox 動的インベントリ連携とネットワーク機器管理は Phase 3）

自宅基盤のデバイス管理（エンドポイント統制・脆弱性可視化・機器/IP 台帳・構成自動化）の設計。選定経緯は [自宅基盤のデバイス管理に Fleet（FleetDM）・NetBox・Ansible を選んだ](decisions/0005-device-management-fleet-netbox.md) および [Apple MDM は Apple Business なしの Jamf Now 無料枠で最小運用に留める](decisions/0009-apple-mdm-jamf-now-scope.md) を参照。

※ 現況（稼働中）は [自宅デバイスの現況](devices.md) と [自宅ネットワークの現況](network.md) を参照。設計と現況を混同しないこと。

## 構成図

![デバイス管理の構成（Phase 1 稼働中）：PC 3台（登録済：Mac / Win / Ubuntu）を ubuntu-desktop 上の Fleet Free v4.95（MySQL 8.4 + Redis 7・SAML SSO）で棚卸し・CVE 検知し、既設ホームルーター・スマート電球（IoT） ×3・iOS 端末 等を NetBox v4.7（PostgreSQL 18 + Valkey 9.1・OIDC SSO）へ投入済み。Apple 2台（macOS ノート PC・iOS 端末）は Jamf Now 無料枠で MDM 管理中。各 DB ダンプは restic で Backblaze B2 へ日次退避済み、将来は NAS・Yamaha 3台と Ansible 連携](diagrams/device-management.png)

- 自己完結 HTML：[diagrams/device-management.html](diagrams/device-management.html)（`caru-ini/stack-diagram` で生成）

## 役割分担（監視基盤との棲み分け）

ツール同士の責務が重複しないよう、5 つの層で役割を分ける。

| 層 | ツール | 担うこと | DB / 出口 |
| --- | --- | --- | --- |
| **監視** | Zabbix | 死活・CPU/メモリ/ディスク・SNMP トラフィック・`restic` 成否通知 | PostgreSQL（`pg_dump`） |
| **端末管理・監査** | **Fleet（FleetDM Free）** | PC 3台・NAS のソフト棚卸し・CVE 脆弱性検知・セキュリティ監査・スクリプト実行・GitOps・API 連携 | MySQL + Redis（アプリ付属例外：`mysqldump` + `fleetctl gitops`） |
| **Apple MDM** | **Jamf Now（無料枠）** | Apple 端末 2台（`macOS ノート PC`・`iOS 端末`）のパスコード強制・FileVault 復旧キー退避・リモートロック／ワイプ | SaaS 管理（Web UI 専用・ローカル DB / API なし） |
| **機器・IP 正本台帳** | **NetBox** | 全機器・Yamaha 3台のポート配線・VLAN・IP アドレス（IPAM）・VM の意図された構成管理 | **PostgreSQL**（`pg_dump`） |
| **構成自動化** | **Ansible** | Ubuntu ホストの OS 設定・ハードニング・Docker・監視・バックアップ・スタック起動順のコード化（Phase 3 で NetBox 動的インベントリ・ネットワーク機器へ拡張） | git 管理（サーバーレス・[ansible/](../ansible/)） |

## 対象デバイス別の管理方針

| 対象 | 区分 | Fleet Free | NetBox | 補足・その他 |
| --- | --- | --- | --- | --- |
| `macOS ノート PC`（M1 / macOS） | 現行 | `fleetd`（棚卸し・CVE・監査・GitOps） | 端末・IP 登録 | MDM は **Jamf Now（無料枠・登録済み）**（監視対象・FileVault PRK 退避、`fleetd` 併用） |
| `workstation-01`（Windows） | 現行 | `fleetd`（棚卸し・CVE・監査・PS 実行） | 端末・固定 IP（`.0.2`） | 手動で Windows MDM 検証可能 |
| ローカル Linux デスクトップ（Ubuntu LTS） | 現行 | `fleetd`（棚卸し・CVE・監査・Shell） | 端末・IP・Docker ホスト | Phase 1 の Docker 実行ホスト、Ansible 対象 |
| `iOS 端末`（iOS） | 現行 | 対象外 | 端末・Wi-Fi VLAN | **Jamf Now（無料枠・登録済み）**（非監視・オープン登録） |
| Yamaha `RTX830` / `SWX2210-8G` / `WLX222` | 将来導入 | 対象外 | 機器・ポート配線・VLAN・IPAM | SSH コンフィグ git 管理、将来 Ansible 連携 |
| `UGREEN NASync DXP4800 Plus` | 将来導入 | 必要に応じてエージェント検討 | 機器・固定 IP・ストレージ構成 | Phase 2 の Docker 集約ホスト |
| `スマート電球（IoT）` ×3 | 現行 | 対象外 | IoT 機器・IoT VLAN 割当 | 死活は Zabbix（ICMP） |
| `remote-vm-01`（外部 VM）（`100.64.0.20`） | 現行（外部） | 必要に応じて Tailscale 経由 `fleetd` | 外部 VM・Tailscale IP | 別リポジトリ（外部 VM バックアップ） で記憶退避（運用中） |

## 配置と段階導入

負担を抑えるため、台帳 → エンドポイント管理 → 自動化の順に段階導入する。

1. **Phase 1（先行検証：ローカル Linux デスクトップ Docker ＋ Jamf Now 無料枠 ＋ Ansible）**
   - **Step 1A（NetBox 先行）— 稼働中**：`ubuntu-desktop` 上の Docker Compose（[stacks/netbox/](../stacks/netbox/)）で **NetBox v4.7 + PostgreSQL 18 + Valkey 9.1** を立ち上げ、`https://netbox.home.example.com` で稼働中。現行の物理デバイス 8 台・外部 VM 1 台（`remote-vm-01`）・IP 9 件を [seed_home.py](../stacks/netbox/seed_home.py) で投入済み、Authentik OIDC SSO 連携まで完了
   - **Step 1B（Fleet Free 導入）— 稼働中**：Docker Compose（[stacks/fleet/](../stacks/fleet/)）で **Fleet Free v4.95.0 + MySQL 8.4 + Redis 7** を立ち上げ、`https://fleet.home.example.com` で稼働中。`macOS ノート PC`・`workstation-01`・`ubuntu-desktop` の PC 3 台すべてに `fleetd`（osquery）を導入・登録済みで、ハードウェア・ソフト棚卸しと Authentik SAML SSO 連携まで完了
   - **Step 1C（Jamf Now 無料枠導入）— 稼働中**：Apple Business を使わず個人 Apple Account の APNs 証明書＋オープン登録で `macOS ノート PC`（自動監視対象）と `iOS 端末`（非監視）の 2 台を登録完了。登録後にオープン登録を無効化し、OS 別ブループリント分離・セキュリティ設定（パスコード要件、Mac の FileVault 有効化と `fdesetup changerecovery -personal` による復旧キー切替・退避）の適用まで確認済み。APNs 証明書の年次更新リマインダー設定済み。Jamf Now には公開 REST API・Webhook・カスタムドメイン・Authentik 標準連携がないため、この最小運用で区切りとする（詳細は [決定記録](decisions/0009-apple-mdm-jamf-now-scope.md)）
   - **Step 1D（Ansible 構築・NetBox 動的インベントリ連携・DR Step 2）— 稼働中**：[ansible/](../ansible/) に 6 ロール（`base_hardening`・`docker_host`・`zabbix_agent`・`backup_and_cron`・`fileserver_usb`・`stacks_deploy`）と読み取り専用ヘルスチェック（`playbooks/health_check.yml`）を構築。平時は NetBox v4.7 の最小権限 RO ユーザー `ansible-ro`（`view` 限定・v2 Bearer トークン・書き込み `403` 検証済み）＋ `netbox.netbox.nb_inventory`（`inventory/netbox.yml`）で動的インベントリ連携し（物理ホームサーバーのみ `infra_hosts` へ自動分類・外部 VM `remote-vm-01` は除外、`primary_ip4` から `host_tailscale_ip` 自動解決）、NetBox 未稼働のベアメタル DR 時は外部コレクション不要の静的インベントリ（`inventory/hosts.yml`）へ切り替えるデュアル構成。管理端末からの `ansible-core 2.22.0` 実機検証（動的／静的両インベントリでの `health_check.yml`・`site.yml --check --diff`・全 9 `/etc/` テンプレートの実機バイト単位完全一致）まで合格済み
2. **Phase 2（本番集約：NAS 導入後）**
   - `DXP4800 Plus` 購入後、NetBox・Fleet を Zabbix と合わせて NAS 上の Docker へ移設・集約する
   - NetBox の PostgreSQL（`pg_dump` + メディアファイル）と Fleet の MySQL（`mysqldump`）の日次 `restic` → Backblaze B2 退避は Phase 1 で先行構築済み（Issue #3 完了）。NAS 導入後は移設先へ引き継ぐ
3. **Phase 3（ネットワーク刷新・自動化連携：Yamaha 3台体制導入後）**
   - `RTX830`・`SWX2210-8G`・`WLX222` のポート・配線・VLAN（Core / Main Wi-Fi / IoT）・IP プレフィックスを NetBox へ登録する
   - 既存 [ansible/](../ansible/)（NetBox 動的インベントリ連携済み）へ NAS / Yamaha ネットワーク機器向けロール・コンフィグバックアップを追加する

## 制約と守り

- **1 端末 1 MDM の原則**：macOS / iOS / Windows に同時に 2 つの MDM を登録することはできない。Apple 2 台（`macOS ノート PC`・`iOS 端末`）の MDM は **Jamf Now 無料枠** に統一し、`macOS ノート PC` の `fleetd`（`osquery`）は MDM とは独立した可視化・CVE 検知として併用する
- **Jamf Now の機能境界**：公開 REST API・Webhook・Active Directory バインド・カスタムドメイン・Authentik 標準 SSO は非対応のため、NetBox / Zabbix / OpenCode との自動連携対象には含めない（APNs 証明書は 1 年ごとに同一の Apple Account で更新する）
- **Fleet の DB 例外と出口**：`docs/database-policy.md` の本線は PostgreSQL だが、Fleet は MySQL + Redis 依存のためアプリ付属の例外とする。ポリシー・クエリ定義は `fleetctl gitops` で git 管理し、DB は `mysqldump` → `restic` → B2 で退避する
- **PostgreSQL メジャーバージョンの固定**：NetBox は Docker イメージ更新時に PostgreSQL のバージョン不整合が起きやすいため、Compose 定義で PostgreSQL のメジャーバージョンを明示固定する
- **秘密情報の扱い**：APNs 証明書・秘密鍵、FileVault 個人復旧キー、NetBox / Fleet の API トークン、DB パスワードはリポジトリへコミットせず、当面はホスト上の `chmod 600 .env` や安全な別保管先で管理する（1Password 導入は一旦保留。詳細は [ID・秘密情報管理の設計](identity-design.md)）

## 未決事項

- **台帳の運用境界**：`docs/devices.md` / `docs/network.md`（Markdown）と NetBox のどちらを一次入力とし、どう同期・役割分担するか
- ※ Phase 1 実行ホスト（`ubuntu-desktop`：Ubuntu 24.04.5 LTS / x86_64 4C/8T CPU / メモリ 11.6 GiB（搭載 12 GB） / SSD 240 GB（実効 234.0 GB））のリソースは実測確認済み。全 17 コンテナ（OpenCode 含む）稼働時点でも空きメモリ約 6.3 GiB・SSD 空き 169 GB と十分な余裕あり
