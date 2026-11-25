# ID・秘密情報管理の設計（Authentik + 1Password）

状態： 構築済み（Phase 1：Authentik 2026.8.5 稼働中・Zabbix / NetBox / Fleet / Infra BI SSO 連携＋MFA 強制・Break-glass 疎通完了／※ 1Password の導入は一旦保留）

自宅基盤のログイン認証（IdP / SSO）と秘密情報・復旧鍵の管理設計。選定経緯は [自宅基盤の ID・秘密情報管理に Authentik と 1Password を選んだ](decisions/0006-identity-authentik-1password.md) を参照。当面の Phase 1 先行構築では、秘密情報はホストの `chmod 600 .env`（git 管理外）と手元のパスワード管理で扱う。

※ 現況（稼働中）は [自宅デバイスの現況](devices.md) と [自宅ネットワークの現況](network.md) を参照。設計と現況を混同しないこと。

## 構成図

![ID・秘密情報管理の構成（Phase 1 稼働中）：管理者・端末から Tailscale + Caddy TLS 経由で Authentik 2026.8（auth.home.example.com + PostgreSQL 16）へ接続し、Zabbix 8.0（SAML 2.0）・Fleet Free v4.95（SAML 2.0）・NetBox v4.7（OIDC）の SSO 連携が稼働中。将来は Yamaha WLX222（RADIUS）や NAS と連携し、PostgreSQL は pg_dump と restic で Backblaze B2 へ退避。1Password は保留とし当面 .env と手元で Break-glass 復旧鍵を管理](diagrams/identity.png)

- 自己完結 HTML：[diagrams/identity.html](diagrams/identity.html)（`caru-ini/stack-diagram` で生成）

## 役割分担（2 層構成）

自宅内で認証をまとめる層と、障害時にも必ず到達できる外部金庫の層を明確に分離する。

| 層 | ツール | 担うこと | 置き場所・出口 |
| --- | --- | --- | --- |
| **IdP / SSO** | **Authentik**（MIT コア） | 管理アプリの SSO（OIDC / SAML 2.0）、MFA / Passkey、Proxy Forward Auth、将来の LDAP / RADIUS 認証 | Docker + **PostgreSQL**（`pg_dump` → `restic` → Backblaze B2） |
| **秘密情報・復旧鍵の金庫** | **1Password** | Break-glass 管理者、DB パスワード、`.env` 秘密値、SNMP 認証情報、API トークン、SSH 鍵、MFA リカバリコード | 外部マネージド SaaS（自宅停止時にも到達可能） |

## 連携対象と認証プロトコル

| 対象 | 区分 | 連携方式 | 備考 |
| --- | --- | --- | --- |
| **Zabbix**（監視基盤） | 稼働中（Phase 1） | **SAML 2.0** | Zabbix 8.0 LTS の DB 証明書保存で連携（`stacks/zabbix/configure_saml.py`）。ローカル `Admin`（`Internal`）を Break-glass として保持 |
| **NetBox**（機器・IP 台帳） | 稼働中（Phase 1） | **OIDC** | `OpenIdConnectAuth` で接続。ローカル `admin` を Break-glass として保持 |
| **Fleet Free**（端末管理） | 稼働中（Phase 1） | **SAML 2.0** | SSRF 対策の Tailscale IP ブロック回避のためインラインメタデータ XML で適用（`stacks/fleet/configure_saml.py`）。`breakglass@home.example.com` を Break-glass として保持 |
| **Grafana OSS（Infra BI）**（データ基盤） | 稼働中（Step 1） | **OIDC** | `https://bi.home.example.com`（`stacks/data-platform/configure_authentik.py`・`authorization_code` / `refresh_token`）。ローカル `breakglass-admin` を Break-glass として保持 |
| **n8n**（ワークフロー自動化） | 稼働中（Step 3） | **Forward Auth（Embedded Outpost）** | `https://n8n.home.example.com`（`stacks/n8n/configure_authentik.py`・`forward_single`）。稼働中 Webhook 2 パス（`/webhook/zabbix-triage`・`/webhook/governance-digest`）のみ `forward_auth` 対象外とし Header Auth で保護 |
| **Yamaha `WLX222`**（無線 AP） | 将来導入 | **RADIUS（802.1X）** | Authentik の RADIUS Outpost と連携し、個人ごとの Wi-Fi 認証検証に活用 |
| **NAS / その他アプリ** | 将来導入 | **LDAP Outpost / Forward Auth** | SSO 非対応の社内ツールや NAS 共有認証を Authentik に寄せる |

## 秘密情報・1Password 運用と守り（鶏と卵問題の回避）

※ 1Password の導入は一旦保留中のため、Phase 1 当面はホスト上の `chmod 600 .env`（git 管理外）と手元のパスワード管理で運用し、将来 1Password へ移行する。

- **自宅障害時の到達性確保**：Ubuntu・NAS・Authentik・自宅回線のいずれが停止しても、手元の環境（将来は 1Password）から復旧用の鍵とパスワードを取り出せる状態を維持する
- **Break-glass アカウント**：Authentik（`akadmin`・TOTP＋静的トークン）・Zabbix（`Admin`）・NetBox（`admin`）・Fleet（`breakglass@home.example.com`）・Grafana（`breakglass-admin`）それぞれに SSO 非依存の緊急用ローカル管理者を 1 つ残し、に全 5 基盤のログイン疎通と手元パスワードマネージャーへの退避を完了（将来は 1Password へ移行）
- **git と秘密情報の分離**：リポジトリには Docker Compose 定義・設定テンプレート・保管場所の参照情報だけを書き、パスワード・API トークン・SNMP コミュニティ名・APNs 鍵・Webhook URL の実値は一切コミットしない
- **Authentik のバックアップ**：`2025.10` 以降の Redis 不要構成（`server`・`worker`・`postgresql`）で運用し、PostgreSQL の `pg_dump` と設定 Blueprint（YAML）を `restic` で Backblaze B2 へ日次退避する（Issue #3）

## HTTPS・リバースプロキシ構成（Caddy + `*.home.example.com`）

選定経緯は [リバースプロキシ・HTTPS 終端の選定記録](decisions/0007-reverse-proxy-caddy-wildcard-tls.md) を参照。

- **窓口の統一**：Docker 上の **Caddy**（`caddy-dns/cloudflare` 組み込みビルド）で `*.home.example.com` を受け、内部コンテナ（`auth.` / `zabbix.` / `netbox.` / `fleet.`）へ振り分ける
- **証明書**：Cloudflare DNS-01 認証で Let's Encrypt のワイルドカード証明書（`*.home.example.com`）を自動取得・更新する（ポート開放不要、CT ログへのサービス名露出も防止）
- **閉域アクセス**：Cloudflare DNS の A レコード（DNS only）を Tailscale IP（`100.64.0.10`）へ向け、管理画面をインターネットへ公開しない
- **`.dev` の制約**：`.dev` は HSTS プリロード対象のため、最初から HTTPS 前提で構築済み。Cloudflare API トークン（`Zone:DNS:Edit` 最小権限）はホストの `.env` と手元（将来は 1Password）で保管する

## 配置と段階導入

認証基盤を先に複雑化すると切り分けが難しくなるため、各基盤がローカル認証で安定してから Authentik を載せる。

1. **Step 0（1Password 集約）— 一旦保留**
   - 導入判断までは、各基盤の `.env`（`chmod 600`・git 管理外）と手元のパスワード管理で秘密情報・復旧鍵を分離管理する
2. **Phase 1（先行検証：ローカル Linux デスクトップ Docker）— 稼働中**
   - `ubuntu-desktop` 上の Docker Compose（[stacks/caddy/](../stacks/caddy/)・[stacks/authentik/](../stacks/authentik/)）で **Caddy**（`*.home.example.com`）と **Authentik 2026.8.5**（PostgreSQL 16）を起動済み
   - **NetBox（OIDC）**・**Zabbix（SAML 2.0）**・**Fleet Free（SAML 2.0）**・**Grafana OSS / Infra BI（OIDC）** の全 4 基盤で SSO ログイン疎通を完了
   - **MFA 強制と Break-glass 検証完了（Issue #2）**：`stacks/authentik/configure_mfa.py` で `default-authentication-mfa-validation` を `webauthn` / `totp` / `static` 限定・未設定時 `configure` 強制へ設定し、`infra-admin`（WebAuthn Passkey＋TOTP＋静的トークン 10 個）と `akadmin`（TOTP＋静的トークン 10 個）の登録・リカバリコード退避・全 5 基盤の Break-glass ローカルログイン疎通を完了（Infra DWH の `mart_identity_posture` で MFA 適用率 `100%`・`dbt test` `PASS=16 / WARN=0` を確認済み）
3. **Phase 2（本番集約：NAS 導入後）**
   - `DXP4800 Plus` 購入後、Caddy・Authentik コンテナ群を NAS 上の Docker へ移設・集約し、`pg_dump` → `restic` → Backblaze B2 の日次退避へ組み込む（先行自動化は Issue #3）
4. **Phase 3（ネットワーク認証拡張：Yamaha 3台体制導入後）**
   - `WLX222` 導入後に Authentik の RADIUS Outpost と連携し、802.1X 認証の検証へ広げる

## 未決事項

- **1Password 導入有無と Vault 設計**：導入する場合の Vault 名（例：`Home Infra`）と項目命名ルール
- ※ Phase 1 実行ホスト（`ubuntu-desktop`：Ubuntu 24.04.5 LTS / x86_64 4C/8T CPU / メモリ 11.6 GiB（搭載 12 GB） / SSD 240 GB（実効 234.0 GB））のリソースは実測確認済み
