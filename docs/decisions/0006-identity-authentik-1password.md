# 自宅基盤の ID・秘密情報管理に Authentik と 1Password を選んだ

状態： 決定済み（設計策定、未構築／※ 1Password の導入は一旦保留）

## 決定

自宅基盤の認証と秘密情報の管理を 2 層に分け、以下を採用する。

- **IdP / SSO 基盤**：**Authentik**（Docker + PostgreSQL、MIT コア）
  - 自宅内の管理アプリ（Zabbix・NetBox・Fleet Free）のログインと MFA を一元化し、将来の Wi-Fi 802.1X（RADIUS）や NAS 認証（LDAP）まで拡張できる構成とする
- **秘密情報・復旧鍵の金庫**：**1Password**（外部マネージド）※ **導入は一旦保留**
  - 本命候補として残しつつ、当面の Phase 1 先行構築ではホスト上の `chmod 600 .env`（git 管理外）と手元のパスワード管理（Apple パスワード等）で運用する

Authentik の配置は監視・デバイス管理基盤と同様に、**先行検証をローカルの Linux デスクトップ（Ubuntu Desktop LTS）上の Docker**、**本番集約を NAS（UGREEN NASync DXP4800 Plus）上の Docker** とする。ただし、管理アプリをローカル認証で安定稼働させてから後段で SSO を載せる順で導入する。

## 要件

- 採用済みの管理基盤（Zabbix・NetBox・Fleet Free）すべてに無料のまま SSO 連携できること
- OIDC・SAML 2.0 だけでなく、将来の Yamaha `WLX222`（802.1X / RADIUS）や NAS（LDAP）にも繋げる拡張性があること
- [データの置き場所の方針](../database-policy.md) に沿い、バックエンド DB が PostgreSQL で `pg_dump` による退避・移行が可能なこと
- 自宅サーバーや Authentik 自体が停止した際にも、復旧に必要なパスワード・SSH 鍵・リカバリコードへ確実に到達できること

## 理由

- **PostgreSQL 単体で完結する構成**：Authentik は `2025.10` 以降 Redis 依存が撤廃され、`server`・`worker`・`postgresql` の 3 コンテナだけで動く。Zabbix・NetBox と同じ PostgreSQL 本線に揃い、`pg_dump` → `restic` → Backblaze B2 のバックアップ経路へそのまま乗る
- **採用済みツールすべてと無料 SSO 連携できる**：Zabbix（SAML 2.0 + JIT）、NetBox（OIDC）、Fleet Free（SAML 2.0）のいずれも公式連携実績があり、SAML と OIDC の双方を実地で学べる
- **ネットワーク・レガシー認証までカバーできる**：無料コアに **LDAP Outpost**・**RADIUS**・**Forward Auth（Proxy Outpost）** が含まれており、将来 `WLX222` の 802.1X 認証や SSO 非対応アプリの保護まで 1 基盤で担える
- **1Password による鶏と卵問題の回避**：秘密情報の正本を自宅外の 1Password に寄せることで、自宅ネットワーク・Ubuntu・NAS・Authentik のどれが落ちても復旧用の鍵と Break-glass アカウントを取り出せる

## 落とした案

- **Keycloak**：100% Apache 2.0・軽量（Quarkus）で企業標準の王道 IdP だが、LDAP サーバー（Outpost）や RADIUS、Forward Auth の内蔵機能を持たない。既存の Active Directory と組む前提の設計寄りであり、自宅の単独基盤（＋将来の 802.1X / NAS LDAP 連携）では Authentik の方が一体で扱いやすいため落とした
- **Microsoft Entra ID Free**：無料でも SSO・MFA は利用できるが、条件付きアクセスや高度な機能は有料（P1 \$7/人/月〜）。自宅 LAN 内限定アプリの認証を外部 SaaS 依存にするとオフライン復旧や構成の取り回しが重くなるため本線から外した
- **Okta Integrator Free Plan**：2025 年に旧 Developer 版から移行し、非本番限定・10 ユーザー上限・180 日非アクティブで自動停止する仕様のため、常時運用する自宅基盤には不向き
- **Vaultwarden（自宅セルフホスト）を唯一の金庫にする案**：NAS や自宅回線が落ちたときに復旧用パスワードへ到達できなくなる（鶏と卵問題）ため、パスワード管理の本線は外部の 1Password へ寄せる判断とした

## 注意

- **Break-glass アカウントの維持**：Zabbix・NetBox・Fleet・Authentik それぞれに SSO 非依存の緊急用ローカル管理者を 1 つ残し、認証情報は 1Password のみで管理する
- **HTTPS 前提**：OIDC / SAML 2.0 および Passkey（WebAuthn）を安全に動かすには HTTPS が必要になるため、導入前に Tailscale HTTPS またはローカルリバースプロキシ（Caddy / Nginx 等）の方式を決める
- **秘密情報は git 禁止**：1Password に保管するパスワード・秘密鍵・API トークン・Webhook URL・SNMP コミュニティ名などの値は一切 git に書かず、ドキュメントには「1Password のどの Vault / 項目にあるか」の参照名のみ記録する
