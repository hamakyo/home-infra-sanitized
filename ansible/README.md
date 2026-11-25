# Ansible 構成管理（home-infra）

状態： 構築済み（NetBox v4.7 動的インベントリ＆静的 DR インベントリ両方で `ubuntu-desktop` 実機検証合格）

素の Ubuntu 24.04 ホストから `ubuntu-desktop` と同等のハードニング・Docker・監視エージェント・バックアップ環境・暫定ファイルサーバー・全 8 スタック起動順までをコードから再現するための Ansible 構成。

## デュアルインベントリ構成（平時と DR の使い分け）

| インベントリ | 用途 | 依存 | 特徴 |
|---|---|---|---|
| `inventory/netbox.yml` | **平時運用**（NetBox v4.7 動的インベントリ） | `netbox.netbox` (`>=3.24.0`) + `pytz` | NetBox の `ansible-ro`（読み取り専用 v2 Bearer トークン）から全アクティブ機器を取得。`sites=home` かつ物理 `role=server` を自動で `infra_hosts` へ分類し、`primary_ip4` から `ansible_host` / `host_tailscale_ip` を自動解決 |
| `inventory/hosts.yml`（既定） | **ベアメタル DR**（NetBox 未稼働時） | **なし**（`ansible.builtin` のみ） | NetBox や外部コレクションがない初期復旧環境でもそのまま実行可能 |

共通変数は `inventory/group_vars/infra_hosts.yml`、ホスト固有の非 IP 属性（`ansible_user`・`host_lan_interface`・`host_fileserver_enabled`）は `inventory/host_vars/ubuntu-desktop.yml` に集約しており、静的・動的のどちらのインベントリでも同じ変数が適用される。

## ロール・Playbook 構成

| 対象 | タグ | `become` | 役割 |
|---|---|---|---|
| `playbooks/health_check.yml` | — | `false` | 読み取り専用の最小ヘルスチェック（SSH ping・OS/カーネル・稼働コンテナ数・ディスク空き容量） |
| `base_hardening` | `base`, `hardening` | `true` | `Asia/Tokyo`、基本パッケージ、`gh` / `tailscale` 導入、SSH 鍵限定、自動再起動（`03:00`）、`ip_nonlocal_bind`、`multi-user.target`、UFW ベースライン |
| `docker_host` | `docker` | `true` | Docker CE + Compose 導入、`infra-admin` グループ追加、`daemon.json` ログローテーション、`tailscaled` 起動待ち、外部 Docker 網 4 本作成 |
| `zabbix_agent` | `monitoring`, `zabbix` | `true` | Zabbix 8.0 LTS `zabbix-agent2` 導入、`docker` グループ追加、`smartctl` sudoers、`10-home-infra.conf`（`host_tailscale_ip` 連動） |
| `backup_and_cron` | `backup`, `cron` | `false` | `restic 0.20.0`（SHA-256 検証付き）導入、状態ディレクトリ作成、ユーザー `crontab`（8 ジョブ）をテンプレートから冪等同期 |
| `fileserver_usb` | `fileserver` | `true` | `host_fileserver_enabled: true` のみ。USB SSD の UAS 無効化 quirk・udev（autosuspend 無効＋`max_sectors_kb=128`）、Samba SMB3、Avahi、UFW `445/tcp` |
| `stacks_deploy` | `stacks` | `false` | `-e infra_stacks_deploy=true` 指定時のみ。`.env` 存在確認、必要に応じ `CADDY_BIND_IP` / `ZBX_BIND_IP` 同期、依存順で `docker compose up -d` |

## 実行方法（macOS ノート PC など管理端末から）

`ansible-core` をシステムへ入れず `uvx` でそのまま実行できる。

### A. 平時運用（NetBox 動的インベントリ）

初回のみ `netbox.netbox` コレクションを導入し、読み取り専用 v2 トークンを管理端末の `~/.config/home-infra/ansible-netbox.env`（権限 `600`・書式は `.env.example` 参照）へ配置する。

```bash
cd ansible

# 初回のみ: NetBox コレクション導入（~/.ansible/collections）
uvx --from ansible-core ansible-galaxy collection install -r requirements.yml

# NetBox 読み取り専用トークンを環境変数へ読み込み
set -a && source ~/.config/home-infra/ansible-netbox.env && set +a

# 1. NetBox からの動的インベントリ確認
uvx --from ansible-core --with pytz ansible-inventory -i inventory/netbox.yml --graph

# 2. 読み取り専用ヘルスチェック（変更なし）
uvx --from ansible-core --with pytz ansible-playbook -i inventory/netbox.yml playbooks/health_check.yml

# 3. 現行 ubuntu-desktop に対する差分確認（sudo あり・変更なし）
uvx --from ansible-core --with pytz ansible-playbook -i inventory/netbox.yml site.yml --check --diff --ask-become-pass

# 4. 非 root ロールのみ確認
uvx --from ansible-core --with pytz ansible-playbook -i inventory/netbox.yml site.yml --tags backup,cron --check --diff
```

### B. ベアメタル DR（静的インベントリ・外部コレクション不要）

```bash
cd ansible

# 1. 構文チェック
uvx --from ansible-core ansible-playbook site.yml --syntax-check

# 2. 読み取り専用ヘルスチェック（静的インベントリ）
uvx --from ansible-core ansible-playbook playbooks/health_check.yml

# 3. 新しい DR マシンへ適用する例（Tailscale IP を上書き）
uvx --from ansible-core ansible-playbook site.yml \
  -i "100.x.y.z," -u infra-admin \
  -e "host_tailscale_ip=100.x.y.z" \
  -e "host_lan_interface=eth0" \
  -e "host_fileserver_enabled=false" \
  --ask-become-pass

# 4. B2 から .env を戻した後、Tailscale IP を .env へ同期して全 8 スタック起動
uvx --from ansible-core ansible-playbook site.yml \
  -i "100.x.y.z," -u infra-admin \
  -e "host_tailscale_ip=100.x.y.z" \
  -e "infra_stacks_deploy=true" \
  -e "infra_sync_bind_ip_in_env=true" \
  --tags stacks
```

## 注意事項

- 秘密情報（`.env`、NetBox RO トークン、B2 鍵、`restic` パスワード、Samba パスワード）は Ansible リポジトリへ一切含めない。DR 時は Backblaze B2 の `infra-db` スナップショットから `.env` を復元してから `--tags stacks` を実行する
- NetBox 側の `ansible-ro` ユーザーは `ObjectPermission` で `view` 権限のみ（トークンも `write_enabled=false` の v2 Bearer トークン）に限定し、書き込み API は `403 Forbidden` で拒否される構成としている
- `fileserver_usb` はディスクフォーマット・`/etc/fstab` 登録・Samba ユーザーパスワード設定を自動実行しない（データ破壊防止のため、初回のみ `stacks/fileserver/setup_host.sh` を明示実行する）
