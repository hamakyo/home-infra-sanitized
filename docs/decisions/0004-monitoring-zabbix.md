# 自宅基盤の監視ツールに Zabbix を選んだ

状態： 決定済み（設計策定、未構築）

## 決定

自宅基盤（ネットワーク機器・端末・NAS・バックアップジョブ）の常時監視基盤として **Zabbix**（Docker + PostgreSQL）を採用する。

- **先行検証環境**：ローカル設置の Linux デスクトップ（Ubuntu Desktop LTS）上の Docker
- **本番配置予定**：NAS（UGREEN NASync DXP4800 Plus）購入後、NAS 上の Docker へ集約する

## 要件

- 将来導入候補の Yamaha 3台体制（`RTX830`・`SWX2210-8G`・`WLX222`）を SNMP で継続監視できること
- PC（Windows / macOS / Linux）・NAS・IoT（スマート電球（IoT） ×3）の死活とリソース、および `restic` バックアップの成否を1か所で監視できること
- 月額費用をかけず、数週間〜数か月の履歴を長期保持できること
- 情シス・社内 SE の実務に直結する運用知見が得られること
- [データの置き場所の方針](../database-policy.md) に沿い、DB の出口（PostgreSQL / `pg_dump`）が明確であること

## 理由

- **「Yamaha + Zabbix」は国内情シスの定番構成**：SNMP テンプレートやトリガー・障害通知の設計知見がそのまま現場で活きる
- **月額ゼロ・長期保持**：セルフホストのためホスト数や保持期間の制限がなく、メトリクスと障害履歴を手元に残せる
- **DB 方針との整合**：バックエンドに **PostgreSQL** を選べるため、`docs/database-policy.md` の本線と一致し、`pg_dump` → `restic` → Backblaze B2 のバックアップ経路へそのまま乗せられる
- **監視方式の幅が広い**：Zabbix Agent 2（OS / Docker / SMART）、SNMP（Yamaha 機器）、ICMP（スマート電球（IoT） や現行ルーター）、Zabbix Trapper / Sender（`restic` や nightly pull のジョブ成否）を1つの UI で統合できる
- **段階導入しやすい**：NAS 購入を待たずにローカルの Linux デスクトップで Docker 構築し、既設ホームルーター・`workstation-01`・macOS ノート PC から先に監視を始められる

## 落とした案

- **Datadog**：機能は網羅的だが企業向けの従量課金 SaaS。2026年時点の無料枠（Basic ホスト最大5台）はメトリクス保持が 1日のみで、Docker コンテナ・ログ管理・SNMP 専用機能（Network Device Monitoring）は無料枠の対象外。自宅の常時運用には月額負担が重いため落とした（ツール自体の学習は 14日間の無料トライアルで別途行う位置づけ）
- **Prometheus + Grafana + Loki**：クラウドネイティブ領域では強力だが、Yamaha 機器の SNMP 監視・しきい値トリガー・障害エスカレーションを一体で管理する情シス実務の学習には Zabbix の方が直接的かつ構成がまとまりやすい
- **LibreNMS**：SNMP ネットワーク監視には強いが、ホストのリソース監視やバックアップジョブ成否のアラート統合は Zabbix に譲る
- **PRTG Freeware**：100センサー上限があり、Windows 常駐前提のため Docker / NAS 集約方針と合わない

## 注意

- Zabbix の Web 管理パスワード、PostgreSQL パスワード、SNMP コミュニティ名、通知用 Webhook URL などの秘密情報は git に記録せず、保管場所のみをドキュメントへ記載する
- Zabbix 自身の停止を見逃さないよう、将来は NAS と別環境（例：外部 VM（remote-vm-01） 側からの外形・死活確認）との相互監視も検討する
