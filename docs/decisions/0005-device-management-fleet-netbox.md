# 自宅基盤のデバイス管理に Fleet（FleetDM）・NetBox・Ansible を選んだ

状態： 構築済み（Phase 1 稼働中：NetBox v4.7・Fleet Free v4.95.0・Jamf Now 無料枠 稼働中／Ansible 未構築。Apple MDM の運用境界は [決定記録](0009-apple-mdm-jamf-now-scope.md) を参照）

## 決定

自宅基盤のデバイス管理を役割ごとに分け、以下の組み合わせを採用する。

- **エンドポイント管理・MDM 層**：**Fleet（FleetDM Free）**（Docker セルフホスト、MIT ライセンス・台数無制限）
  - `osquery` + `fleetd` で `macOS` / `Windows` / `Linux` の 3OS を一元可視化し、ソフトウェア棚卸し・CVE 脆弱性検知・セキュリティポリシー監査・スクリプト実行・GitOps 管理を担う
- **Apple MDM 検証（補助・任意）**：**Jamf Now（無料枠）**
  - Apple 端末 3台まで無料のため、`iOS 端末` 等で紛失モード・リモートロック・Blueprint など Apple MDM 特有の運用を検証する枠として併用検討する（同一端末への MDM 二重登録は不可）
- **機器・IP・配線台帳層（DCIM / IPAM）**：**NetBox**（Docker + PostgreSQL）
  - Yamaha 3台体制・VLAN・IP アドレス・物理配線・PC / NAS / VM の「意図された構成の正本」として管理する
- **構成自動化**：**Ansible**（エージェントレス・コード管理）
  - NetBox をインベントリ起点とし、端末・NAS・ネットワーク機器の設定反映を段階的に自動化する

配置は監視基盤（Zabbix）と同様に、**先行検証をローカルの Linux デスクトップ（Ubuntu Desktop LTS）上の Docker**、**本番集約を NAS（UGREEN NASync DXP4800 Plus）上の Docker** とする。

## 要件

- 自宅の主要 OS（`macOS` / `Windows` / `Ubuntu`）と将来の Yamaha 3台体制・NAS・`remote-vm-01`（外部 VM） を無理なくカバーできること
- 月額費用をかけず自宅常駐でき、情シス・社内 SE の実務に直結する設計・運用知見が得られること
- 監視基盤（Zabbix）と役割が重複しすぎず、「監視」「端末管理」「構成台帳」の責務が明確であること
- [データの置き場所の方針](../database-policy.md) と整合し、バックアップおよび移行の出口が確保されていること

## 理由

- **役割分担が明確**：監視は **Zabbix**（死活・リソース・SNMP・ジョブ成否）、端末内部の統制と脆弱性可視化は **Fleet**（`osquery`・MDM・ポリシー監査）、機器とネットワークの正本台帳は **NetBox**（DCIM・IPAM）、設定反映は **Ansible** と棲み分けられる
- **クロスプラットフォームでモダン情シスに直結**：Fleet Free は 1 つの管理画面で `macOS` / `Windows` / `Linux` の脆弱性検知・ポリシー監査・GitOps（`fleetctl gitops`）まで扱え、手動登録による Apple / Windows MDM の仕組みも学べる
- **PostgreSQL 方針と NetBox の完全一致**：NetBox のバックエンドは PostgreSQL（15+）のみのため、Zabbix と同じ `pg_dump` → `restic` → Backblaze B2 の運用へそのまま載せられる
- **Apple MDM の実務感覚も無料で補える**：Jamf Now は 3台まで無料のため、`iOS 端末` や `macOS ノート PC` の台数なら追加費用ゼロで Jamf の運用感覚を試せる
- **段階導入しやすい**：NAS や Yamaha 機器の購入を待たず、手元の Ubuntu デスクトップ Docker から NetBox → Fleet の順に小さく始められる

## 落とした案

- **Microsoft Intune**：企業標準の統合 MDM だが有料ライセンス前提。自宅の常時運用には月額負担が重いため落とした
- **Jamf Pro（Jamf for Mac / Mobile）**：Apple MDM の企業定番だが最低 25台・年額契約前提のため個人自宅には不向き
- **Snipe-IT**：備品・保証・貸出管理の定番 OSS だが、公式サポート DB が MariaDB / MySQL（PostgreSQL は非公式）であり、VLAN・IPAM・物理配線管理は対象外。自宅規模のネットワーク刷新と合わせて学ぶなら NetBox の方が価値が高い
- **GLPI**：資産管理からヘルプデスクまで一体だが PHP + MySQL のモノリス構成で、個人宅の運用には機能が過多
- **phpIPAM**：IP アドレス管理単機能としては軽いが、DCIM（物理機器・ポート配線）や Ansible 連携まで見据えると NetBox に譲る

## 注意

- **同一端末への MDM 二重登録は不可**：Apple / Windows の MDM は 1 端末につき 1 管理サーバーが原則。`macOS ノート PC` を Fleet の Apple MDM へ登録するか Jamf Now へ登録するかは構築時に一方へ絞る（Fleet の `osquery` エージェントによる可視化自体は MDM とは別に併用可能）
- **Fleet の DB 例外と出口**：Fleet は MySQL + Redis 依存のため、[データの置き場所の方針](../database-policy.md) の PostgreSQL 本線に対するアプリ付属の例外として扱う。設定は `fleetctl gitops` で YAML 化し、DB は `mysqldump` → `restic` → Backblaze B2 で出口を確保する
- **Fleet Free の機能上限**：ゼロタッチ登録（ABM / Windows Autopilot）、アプリ自動配布、FileVault / BitLocker の強制適用と復旧鍵エスクロー、ラベル単位の個別配布は有料版（Fleet Premium、\$7/台/月）の機能。無料版では可視化・監査・手動 MDM・スクリプト実行を前提とする
- **秘密情報の扱い**：APNs 証明書・秘密鍵、NetBox / Fleet の API トークン、DB パスワード等は git へ記録せず、保管場所のみドキュメントへ記載する
