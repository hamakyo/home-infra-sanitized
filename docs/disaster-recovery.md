# 障害復旧手順書（Disaster Recovery Runbook）

状態： 構築済み（B2 `verify` フェーズ実機検証済み）

`ubuntu-desktop` 本体が物理故障・OS 破損した際に、別の Ubuntu 24.04 LTS マシンへ GitHub（コード＋Ansible）と Backblaze B2（暗号化 DB 7 本・`.env` 11 個・ホスト設定・重要ファイル）から自宅基盤一式を復旧する手順。

## 前提・目標値・手元に控える秘密

| 項目 | 内容 |
|---|---|
| **RPO（データ損失の上限）** | 最大 24 時間（毎朝 `01:30 JST` DB 7 本＋`.env` 11 個＋ホスト設定、毎朝 `01:45 JST` `/mnt/files/important`、毎朝 `02:00 JST` Repolica Git 複製） |
| **RTO（復旧所要時間の目安）** | 新 Ubuntu 24.04 マシン準備後 約 30〜60 分 |
| **手元に必須の秘密（これ 1 組だけ）** | **Backblaze B2 Application Key（Key ID / Application Key）＋ `restic` パスワード**（保管場所：自宅外の手元控え） |
| **その他ログインが必要な外部サービス** | GitHub（`example-user/home-infra-sanitized` 取得）・Tailscale（新ノード参加・旧ノード削除）・Cloudflare（`*.home.example.com` の A レコード切替） |
| **復旧対象（全 21 コンテナ＋ホスト基盤）** | Caddy・Authentik・Zabbix 8.0 LTS・NetBox v4.7・Fleet Free・Infra DWH / Grafana / RAG MCP・n8n・OpenCode・（任意で暫定ファイルサーバー Samba） |

## 復旧フロー全体像

1. **新 Ubuntu 24.04 マシン準備**（SSH 公開鍵・Tailscale 参加・Key Expiry 無効化）
2. **Ansible で OS 土台を一括構築**（ハードニング・Docker・Zabbix Agent・`restic`・cron）
3. **B2 からスナップショット取得＆`.env` 11 個復元**（新 Tailscale IP を `CADDY_BIND_IP` / `ZBX_BIND_IP` へ自動同期）
4. **DB 7 本＋NetBox メディア自動復元＆全 8 スタック起動**（PostgreSQL 最小権限ロール再付与込み）
5. **外部 3 点の切替**（Cloudflare DNS・`gh` CLI 認証・任意で USB SSD マウント）
6. **ヘルスチェック＆一時ファイル削除**
7. **旧故障ノードの整理**（Tailscale 管理画面）

---

## Step 1：新 Ubuntu 24.04 マシンの初期準備

新マシンに Ubuntu 24.04 LTS（ユーザー名 `infra-admin`・OpenSSH Server 有効）を入れ、macOS ノート PC から SSH 鍵ログインできるようにする。

1. 新マシンへ Tailscale を導入して参加する：
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   tailscale ip -4   # 新しい Tailscale IP を控える（以下 <NEW_TS_IP>）
   ```
2. Tailscale 管理画面で新ノードの **`Disable key expiry`** を有効化する
3. 新マシンで `example-user/home-infra-sanitized` を clone する（管理端末からの Ansible 実行前にリポジトリを配置）：
   ```bash
   mkdir -p ~/dev
   git clone https://github.com/example-user/home-infra-sanitized.git ~/dev/home-infra
   ```

## Step 2：管理端末（macOS ノート PC）から Ansible で土台構築

macOS ノート PC 上で `example-user/home-infra-sanitized/ansible` へ移動し、新マシンの Tailscale IP と有線 NIC 名（`ip -br link` で確認）を指定して適用する。外部コレクションや事前インストールは不要。

```bash
cd ~/dev/home-infra/ansible

uvx --from ansible-core ansible-playbook site.yml \
  -i "<NEW_TS_IP>," -u infra-admin \
  -e "host_tailscale_ip=<NEW_TS_IP>" \
  -e "host_lan_interface=<LAN_NIC>" \
  -e "host_fileserver_enabled=false" \
  --ask-become-pass
```

※ USB SSD（暫定ファイルサーバー）も引き継ぐ場合は `host_fileserver_enabled=true` とする。

この 1 コマンドで次がすべて完了する：
- `Asia/Tokyo`・SSH 鍵限定・UFW・`unattended-upgrades`（`03:00` 自動再起動）・`ip_nonlocal_bind`・`multi-user.target`
- Docker Engine + Compose・ログローテーション・`tailscaled` 起動待ち・外部 Docker 網 4 本
- `zabbix-agent2`（新 Tailscale IP 反映済み）・`smartctl` sudoers
- `restic 0.20.0`（SHA-256 検証付き）・状態ディレクトリ・ユーザー `crontab`

## Step 3：Backblaze B2 からスナップショット取得＆`.env` 11 個の復元

新マシンへ SSH ログインし、手元の B2 鍵と `restic` パスワードを **シェル履歴へ残らない形** で一時エクスポートしてから [stacks/backup/restore_dr.py](../stacks/backup/restore_dr.py) を実行する。

```bash
# 先頭に半角スペースを入れるか read -s で入力し履歴に残さない
read -r -p "AWS_ACCESS_KEY_ID: " AWS_ACCESS_KEY_ID
read -r -s -p "AWS_SECRET_ACCESS_KEY: " AWS_SECRET_ACCESS_KEY; echo
read -r -s -p "RESTIC_PASSWORD: " RESTIC_PASSWORD; echo
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY RESTIC_PASSWORD
export RESTIC_REPOSITORY="s3:https://s3.<B2_REGION>.backblazeb2.com/backup-home-infra-example/infra-db"

# 1. B2 から最新スナップショットを展開し全ファイルの SHA-256 を検証
python3 ~/dev/home-infra/stacks/backup/restore_dr.py \
  --phase fetch \
  --workdir /tmp/dr-restore

# 2. 全 10 個の .env を chmod 600 で配置し、CADDY_BIND_IP / ZBX_BIND_IP を新 IP へ更新
python3 ~/dev/home-infra/stacks/backup/restore_dr.py \
  --phase env \
  --workdir /tmp/dr-restore \
  --tailscale-ip "<NEW_TS_IP>"
```

## Step 4：全 7 DB＋NetBox メディアの自動復元＆全 8 スタック起動

```bash
python3 ~/dev/home-infra/stacks/backup/restore_dr.py \
  --phase db \
  --workdir /tmp/dr-restore \
  --confirm-db-overwrite
```

内部で以下を順番に自動実行する：
1. 5 つの DB コンテナ（`zabbix-postgres`・`netbox-postgres`・`authentik-postgres`・`fleet-mysql`・`infra-dwh-postgres`）を起動して `healthy` 待機（初回起動時に `infra-dwh-postgres` のロール `etl_writer` / `bi_reader` / `rag_reader` / `grafana_app` / `n8n_app` / `n8n_ro` も自動作成される）
2. 7 本の DB（`zabbix`・`netbox`・`authentik`・`infra_dwh`・`grafana_app`・`n8n_app`・`fleet`）を再作成してダンプ投入し、`infra_dwh` と `n8n_app` の最小権限グラントを再適用
3. 依存順（`caddy` → `authentik` → `zabbix` → `netbox` → `fleet` → `data-platform` → `n8n` → `opencode`）で全 8 スタック（21 コンテナ）を起動
4. `netbox` の `media` / `reports` / `scripts` を `docker cp` で復元

完了後、平文の `.env` が含まれる作業ディレクトリと環境変数を即座に消去する：

```bash
rm -rf /tmp/dr-restore
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY RESTIC_PASSWORD RESTIC_REPOSITORY
```

## Step 5：外部設定 3 点の切替

1. **Cloudflare DNS**：`*.home.example.com` の A レコードを `<NEW_TS_IP>` へ変更（Caddy が Let's Encrypt 証明書を自動再取得する）
2. **GitHub CLI（日曜巡回ジョブ・`git pull` 用）**：新マシンで再認証する（`hosts.yml` はセキュリティ方針上バックアップ対象外）
   ```bash
   gh auth login --insecure-storage
   gh auth setup-git
   ```
3. **暫定ファイルサーバー（必要な場合のみ）**：
   - 無事な USB SSD を繋ぎ替える場合は `sudo bash ~/dev/home-infra/stacks/fileserver/setup_host.sh` を実行（既存 `ext4` ラベル `home-files` は消去せず自動マウント＋Samba ユーザー再設定）
   - USB SSD 自体も破損して新品へ交換した場合は、フォーマット後に `/mnt/files/important` を B2 から復元する：
     ```bash
     python3 ~/dev/home-infra/stacks/backup/run_restic.py --fileserver restore latest --target /tmp/restore-fileserver
     ```
4. **インベントリ更新**：新マシンの IP・ホスト名で [ansible/inventory/hosts.yml](../ansible/inventory/hosts.yml) と NetBox を更新する

## Step 6：復旧後ヘルスチェック

```bash
# 全 21 コンテナが Up / healthy であること
docker ps --format "table {{.Names}}\t{{.Status}}"

# 主要 HTTPS エンドポイント応答確認（DNS 反映後）
for u in auth zabbix netbox fleet bi n8n opencode; do
  printf "%-10s " "$u"
  curl -ksS -o /dev/null -w "%{http_code}\n" "https://${u}.home.example.com"
done

# バックアップと Zabbix 監視の健全性確認
python3 ~/dev/home-infra/stacks/backup/backup_phase1_db.py --dry-run
```

## Step 7：旧マシンの後始末

- Tailscale 管理画面から故障した旧ノードを削除する
- Zabbix / Fleet のホスト一覧で旧ノードが重複していれば整理する

## 定期的な復旧検証（実機で確認済み）

本番稼働中の `ubuntu-desktop` を一切破壊せず、B2 スナップショットの整合性と復号可能性をいつでも検証できる：

```bash
python3 ~/dev/home-infra/stacks/backup/restore_dr.py --phase verify
```
