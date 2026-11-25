# 自宅基盤のリバースプロキシと HTTPS 終端に Caddy と *.home.example.com を選んだ

状態： 構築済み（`ubuntu-desktop` で稼働中）

## 決定

自宅基盤の管理アプリ群（Authentik・Zabbix・NetBox・Fleet Free）の HTTPS 窓口として、**Caddy**（Docker、Cloudflare DNS プラグイン込みビルド）と所有ドメイン **`*.home.example.com`** を採用する。

- **サブドメイン構成（予定）**：公開用途の `example.com` と分離し、`*.home.example.com` 配下へ集約する
  - `auth.home.example.com` → Authentik
  - `zabbix.home.example.com` → Zabbix Web
  - `netbox.home.example.com` → NetBox
  - `fleet.home.example.com` → Fleet Free
- **証明書取得**：**Cloudflare DNS-01 認証**により、Let's Encrypt のワイルドカード証明書（`*.home.example.com`）を自動取得・自動更新する（`example.com` の権威 DNS は Cloudflare 稼働を確認済み）
- **名前解決とアクセス制限**：Cloudflare DNS の A レコードはプロキシ OFF（DNS only）で Ubuntu / NAS の **Tailscale IP（`100.x.y.z`）** に向ける（将来 `RTX830` 導入後は自宅内 DNS で LAN IP へ解決する構成も併用可）。インターネット側へのポート開放や公開は行わない

配置は他の管理基盤と同様に、**先行検証をローカルの Linux デスクトップ（Ubuntu Desktop LTS）上の Docker**、**本番集約を NAS（UGREEN NASync DXP4800 Plus）上の Docker** とする。

## 要件

- 上流ルーター配下の二重 NAT 環境のまま、ポート開放なしで正規 HTTPS 証明書を自動更新できること
- Authentik の OIDC / SAML 2.0・Passkey（WebAuthn）や `.dev` ドメインの HSTS 要件を満たすこと
- 管理画面をインターネットへ公開せず、自宅・外出先（Tailscale 経由）の双方から同じ URL で到達できること
- 設定ファイルを git で簡潔に管理でき、初期導入と運用の負担が小さいこと

## 理由

- **自動 HTTPS が本体で完結する**：Caddy は `Caddyfile` 数行でリバースプロキシと Let's Encrypt 証明書の自動取得・更新が完結し、別ツール（Certbot 等）の定期更新管理が不要
- **Cloudflare DNS との相性**：`example.com` がすでに Cloudflare DNS で管理されているため、DNS-01 認証を使えばポート 80/443 を外部開放せず二重 NAT のままワイルドカード証明書を取れる
- **CT ログでの情報露出を最小化できる**：サービスごとの個別証明書ではなく `*.home.example.com` のワイルドカード証明書 1 枚にまとめることで、Certificate Transparency（CT）公開ログに稼働ツール名（`zabbix.` 等）が載るのを防げる
- **所有ドメインで一貫した URL 設計ができる**：ポート番号の暗記が不要になり、Authentik・Zabbix・NetBox・Fleet の連携設定（ACS URL / Redirect URI）も綺麗に揃う

## 落とした案

- **Nginx（＋ Certbot）**：企業・情シスで最も広く使われる王道だが、リバースプロキシ設定と証明書更新（Certbot）を分けて組む手間がある。まず運用負荷の低い Caddy で基盤全体を立ち上げ、Nginx 自体の設定学習が必要になった段階で別途比較・検証する位置づけとした
- **Nginx Proxy Manager**：Web GUI で扱いやすいが、設定状態が DB 側に寄るため `Caddyfile` のように git で差分管理しにくい
- **Traefik**：Docker ラベルによる自動検出が強力だが、初手の設定概念が Caddy より重いため見送った
- **Tailscale Serve（`*.ts.net`）**：設定は最も軽いが、URL が Tailnet 固有ドメインに固定され所有ドメイン `example.com` を活かせない
- **Cloudflare Tunnel（外部公開）**：管理画面を自宅外ネットワークへ公開する必要がなく、Tailscale 閉域接続の方が攻撃面を小さく保てるため採用しない

## 注意

- **`.dev` ドメインは HSTS プリロード対象**：主要ブラウザが HTTPS 接続を強制するため、平文 HTTP での仮アクセスはできない。最初から Caddy＋DNS-01 で HTTPS を通す前提で構築する
- **Cloudflare プラグインの組み込み**：公式の標準 Caddy イメージには Cloudflare DNS モジュールが入っていないため、`xcaddy` を使った数行の `Dockerfile` で `github.com/caddy-dns/cloudflare` 込みのイメージをビルドして使う
- **API トークンの最小権限と保管先**：Cloudflare API トークンは `example.com` の「Zone:Zone:Read」「Zone:DNS:Edit」権限のみに絞り、実値は **1Password** で保管して git にはコミットしない
