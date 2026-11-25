# バックアップ体制の設計

状態： 構築済み（Phase 1：`ubuntu-desktop` の管理基盤 DB 7 本＋`.env` 11 個＋ホスト設定＋暫定ファイルサーバー `/mnt/files/important` の B2 日次退避＋Repolica Git 日次複製が稼働中／Phase 2 の本番 NAS 連携は未構築）
前提： NAS は UGREEN NASync DXP4800 Plus + WD Red Plus 8TB ×4 の RAID6（実効 16TB）。選定理由は decisions を参照。

## 守る対象と所在

| データ | 正本 | コピーの現状 |
|---|---|---|
| 各種データセットの別リポジトリ | GitHub | 外部 VM（remote-vm-01） の clone（一部のみ） |
| Mac のファイル | Mac 本体 | なし |
| モバイル端末の写真 | モバイル端末 / クラウド | クラウドのみ |
| 生成画像など | workstation-01（Windows） | なし |
| 外部 VM の記憶とプロファイル | 外部 VM（remote-vm-01） | 別リポジトリ 経由で GitHub（運用中） |
| 管理基盤 DB 7 本・全スタック `.env` 11 個・ホスト設定 | `ubuntu-desktop` | Backblaze B2 へ `restic` 暗号化で日次退避（運用中・復元訓練済み・Issue #3） |
| Git リポジトリ（GitHub `infra-admin`） | GitHub → `ubuntu-desktop`（ローカルミラー）→ GitLab.com（`example-gitlab`） | `example-user/example-repo-mirror` で毎朝 `02:00 JST` 日次複製＆ref 検証（運用中・初期 2 本・別リポジトリ（個人情報系） 除外・Zabbix 監視） |
| 暫定ファイルサーバー重要データ（`/mnt/files/important`） | `ubuntu-desktop`（USB SSD） | Backblaze B2 へ `restic` 暗号化で日次退避（運用中・復元訓練済み・`/mnt/files/share` は対象外） |

## 流れ

一方向に寄せる。双方向の同期は正本を割るので使わない。

- **GitHub → NAS**：全 repo のミラーを nightly で pull する。NAS 側からは書き込まない
- **Mac → NAS**：Time Machine の保存先を NAS にする（世代バックアップはローカルで完結）
- **workstation-01 → NAS**：生成画像だけ NAS の共有へ。モデル類は再ダウンロード可能なので対象外
- **NAS → Backblaze B2**：NAS にしかないデータを restic で送る。下記ジョブ設計

## NAS → B2 ジョブ設計

- 対象：写真、書類、メディアの原本、NAS の設定
- 対象外：repo のミラー（GitHub が正本）、Time Machine（バックアップのバックアップになるだけ）
- 道具：restic。暗号化、重複排除、世代管理が1つで済み、B2 の S3 互換エンドポイントに書ける。置き先を将来替えても道具は変わらない
- 実行場所：NAS の Docker（常駐 agent の隣）
- 頻度：夜間1回
- 保持：日次7、週次4、月次12を目安に prune する
- 前提ルール：Mac ローカルにしかない重要ファイルは NAS の共有に置く。これがないと、家ごと失うケースで Mac のファイルだけ穴になる

## 守り

- B2 のアプリケーションキーはバックアップ専用に発行し、対象を1バケットに絞る。NAS が侵害されても鍵の権限の外は触れない
- restic のパスワードは NAS の中だけに置かない。パスワードマネージャにも控える（保管場所のみここに記す：未設定）。パスワードを失うと暗号化したコピーは全部読めなくなる
- NAS 側でスナップショットを有効にし、誤削除とランサムウェアの初動はローカルで巻き戻せるようにする
- クラウド側のコピーは3つのコピーの1つでしかない。アカウント停止のような事態でも NAS と GitHub が残り、restic のリポジトリは別の置き先へ移せる

## 復旧の確認

- 月1回、restic check とファイル1件の復元テストを行う。常駐 agent の定例仕事にする
- 復元できたことのないバックアップはバックアップと数えない

## コスト

- B2 の従量は $6.95/TB/月（10GB まで無料枠）。Phase 1（管理基盤 DB 7 本＋`.env` 11 個＋ホスト設定）の実容量は 2 スナップショット計 `約 30 MiB`（圧縮率 `4.93x`）、暫定ファイルサーバー `/mnt/files/important` は `数 KiB` でいずれも無料枠内。Phase 2 の NAS 全対象を加えても 1TB 未満の見込み

## Phase 1 先行稼働：管理基盤 DB・秘密設定・ホスト設定 → Backblaze B2（稼働中）

NAS 導入前でも `ubuntu-desktop` 本体が故障した際に別マシンから復元できるよう、全 7 DB のダンプに加えて全スタックの `.env` とホスト復旧用設定を `restic`（クライアント側暗号化）で Backblaze B2 へ日次退避している（Issue #3・DR Step 1 完了）。

- **実装**：[stacks/backup/backup_phase1_db.py](../stacks/backup/backup_phase1_db.py)（排他ロック・一時ステージング `chmod 700` / `.env` `chmod 600` 自動削除・30日 JSON ログ保持）、手動 `restic` 実行ラッパ [stacks/backup/run_restic.py](../stacks/backup/run_restic.py)
- **ツール**：`restic 0.20.0`（`/home/infra-admin/.local/bin/restic`・SHA-256 検証済み）
- **対象データ**：
  - **管理基盤・データ基盤 DB（7 本）**：
    - Zabbix 8.0 LTS：`zabbix-postgres`（PostgreSQL 16・`pg_dump --format=plain --no-owner --no-acl`）
    - NetBox v4.7：`netbox-postgres`（PostgreSQL 18・`pg_dump`）＋ `netbox` コンテナの `media` / `reports` / `scripts`
    - Authentik 2026.8.5：`authentik-postgres`（PostgreSQL 16・`pg_dump`）
    - Fleet Free v4.95.0：`fleet-mysql`（MySQL 8.4・`mysqldump --single-transaction --routines --triggers --quick --hex-blob --set-gtid-purged=OFF`）
    - Infra DWH / Grafana / n8n：`infra-dwh-postgres`（PostgreSQL 17・`infra_dwh`・`grafana_app`・`n8n_app`）
  - **全スタック `.env`（11 ファイル・`secrets/env/stacks/**/.env`）**：`authentik`・`backup`・`caddy`・`data-platform`・`fleet`・`n8n`・`netbox`・`opencode`・`opencode/jobs`・`repolica`・`zabbix`（`AUTHENTIK_SECRET_KEY` や `N8N_ENCRYPTION_KEY` など DB 復号に必須の鍵を含む。`manifest.json` にはパス・サイズ・SHA-256 のみ記録し中身は一切ログ出力しない）
  - **ホスト復旧用設定（`host/`）**：ユーザー `crontab`（9 ジョブ）・`~/.ssh/authorized_keys`・カスタム `/etc/` 設定 12 ファイル（`fstab`・`smb.conf`・`samba.service`・`sshd` ハードニング・`52unattended-upgrades-local`・`sysctl`・`NetworkManager` `eno1` `e1000e` チューニング・`docker`/`smbd` systemd override・USB SSD quirk/udev・`zabbix_agent2.d/10-home-infra.conf`）・稼働中 21 コンテナの正確なイメージ一覧（`host_meta.json`）
  - **意図的に対象外としたもの**：`~/.config/gh/hosts.yml`（GitHub OAuth トークンは被害範囲を狭めるため退避せず、新マシンで `gh auth login` により再発行する）
  - 各ファイルのサイズ・SHA-256・イメージタグを記録した `manifest.json`
- **リポジトリ**：`s3:https://s3.<B2_REGION>.backblazeb2.com/backup-home-infra-example/infra-db`（タグ：`phase1-db`）
- **実行スケジュール（`ubuntu-desktop` cron）**：
  - 毎日 `01:30 JST`：`backup_phase1_db.py`（DB ダンプ＋`.env`/ホスト設定収集 → `restic backup` → `restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune` → Zabbix Trapper 送信）
  - 毎月1日 `02:30 JST`：`backup_phase1_db.py --check`（`restic check --read-data-subset=10%`）
- **秘密情報の保管場所**：全 `.env` が暗号化退避されているため、自宅外の手元控えとして必須なのは **「B2 Application Key と `restic` パスワードの 1 組だけ」**（ホスト上では `~/dev/home-infra/stacks/backup/.env` `chmod 600`・git 管理外）
- **監視連携**：`ubuntu-desktop` の Zabbix Trapper（`backup.phase1_db.*` 5アイテム・失敗 / 28時間未成功の 2トリガー）へ毎回送信し、異常時は Discord へ通知（設定スクリプト：[stacks/zabbix/configure_backup_monitoring.py](../stacks/zabbix/configure_backup_monitoring.py)）

### 復元手順（Phase 1 DB・`.env`・ホスト設定）

1. スナップショットを確認し、`umask 077` の一時ディレクトリへ展開する（`.env` にシェル記号が含まれる場合も安全に扱うため `run_restic.py` 経由で実行）：
   ```bash
   umask 077
   python3 ~/dev/home-infra/stacks/backup/run_restic.py snapshots --compact
   python3 ~/dev/home-infra/stacks/backup/run_restic.py restore latest --target /tmp/restore-phase1
   ```
2. 展開先の `home/infra-admin/.local/state/infra-backup/staging/manifest.json` の SHA-256 と各 `.sql`・`.env`・ホスト設定を照合する
3. 全スタックの `.env` を `secrets/env/stacks/` からリポジトリ内の所定パスへ `chmod 600` を保って戻し、必要に応じて `host/crontab.txt`・`host/ssh/authorized_keys`・`host/etc/` を反映する
4. PostgreSQL 系（Zabbix / NetBox / Authentik / `infra_dwh` / `grafana_app` / `n8n_app`）は対象 DB へ `psql -v ON_ERROR_STOP=1 -U <user> -d <db> < <name>.sql` で復元、Fleet（MySQL 8.4）は `mysql -u root -p fleet < fleet.sql` で復元する
5. NetBox は必要に応じて `media` / `reports` / `scripts` を `docker cp` で戻し、復元完了後に `/tmp/restore-phase1` を確実に削除する

### 初回リストア訓練の記録

- **対象スナップショット**：`<snapshot-id-initial>`（`ubuntu-desktop`）
- **容量・整合性**：
  - 未圧縮ダンプ合計：`約 60 MiB`（Zabbix 約 50 MB / NetBox 約 1 MB / Authentik 約 6 MB / Fleet 約 7 MB）
  - B2 実保存容量（`restic stats --mode raw-data`）：`約 13 MiB`（圧縮率 `約 5x`・`約 80%` 削減）
  - `restic check --read-data`：全 2パック検証、`no errors were found`
  - `restic restore latest`：3秒で全 21ファイル/ディレクトリ展開完了、4 DB すべての SHA-256 が `manifest.json` と一致
- **実 DB 復元テスト（使い捨てコンテナへ投入）**：
  - **NetBox（`postgres:18-alpine`）**：`netbox.sql` をエラーなしで全件復元、`dcim_device = 8`・`ipam_ipaddress = 9` を確認
  - **Fleet（`mysql:8.4`）**：`fleet.sql` をエラーなしで全件復元、`hosts = 3`・`software = 4074` を確認

### DR Step 1 拡張リストア訓練の記録

- **対象スナップショット**：`<snapshot-id-dr>`（`ubuntu-desktop`・所要 `67s`・Zabbix Trapper `5/5` 送信成功）
- **対象内訳・整合性**：
  - 未圧縮合計：`約 110 MiB`（DB 7 本＋NetBox 3 ディレクトリ＋`.env` 10 ファイル＋`/etc/` 11 ファイル＋`crontab`＋`authorized_keys`＋`host_meta.json`）
  - B2 実保存容量（`restic stats --mode raw-data`・2 スナップショット計）：`約 30 MiB`（未圧縮 `約 150 MiB` → 圧縮率 `約 5x`・`約 80%` 削減）
  - `restic restore latest`：5 秒で全 84 ファイル/ディレクトリを展開し、DB 7 本・`.env` 10 ファイル・`/etc/` 11 ファイル・`crontab`・`authorized_keys`・`host_meta.json` すべての SHA-256 が本番実ファイルおよび `manifest.json` と完全一致（検証後 `/tmp/restore-step1-drill` を即時削除）
  - `restic check --read-data-subset=10%`：全 2 スナップショット検証、`no errors were found`

## 暫定ファイルサーバー重要データ → Backblaze B2（稼働中）

単体 USB SSD（`/mnt/files`）の故障・誤削除に備え、重要ファイル専用ディレクトリ `/mnt/files/important` を既存 B2 バケット内の独立リポジトリへ日次暗号化退避している（構成全体は [docs/fileserver.md](fileserver.md) を参照）。

- **実装**：[stacks/backup/backup_fileserver.py](../stacks/backup/backup_fileserver.py)（排他ロック `fileserver.lock`・`/mnt/files` マウント確認ガード・30日 JSON ログ保持）、手動ラッパ [stacks/backup/run_restic.py](../stacks/backup/run_restic.py)（`--fileserver` オプション）
- **対象 / 対象外**：
  - 対象：`/mnt/files/important` のみ（OS 自動生成ファイル `.DS_Store`・`._*`・`Thumbs.db`・`desktop.ini` は除外）
  - 対象外：`/mnt/files/share`（B2 容量課金の肥大化を防ぐため意図的に除外）
- **リポジトリ**：`s3:https://s3.<B2_REGION>.backblazeb2.com/backup-home-infra-example/fileserver-important`（タグ：`fileserver-important`、`infra-db` と独立したロック／prune ライフサイクル。B2 鍵と `restic` パスワードは既存の `stacks/backup/.env` を共用）
- **安全ガード**：実行前に `os.path.ismount("/mnt/files")` と `/mnt/files/important` の存在を検証し、USB SSD 未マウント時は空スナップショットを作らず即座に失敗扱いとして Zabbix へ通知する
- **実行スケジュール（`ubuntu-desktop` cron）**：
  - 毎日 `01:45 JST`：`backup_fileserver.py`（マウント検証 → `restic backup` → `restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune` → Zabbix Trapper 送信）
  - 毎月1日 `02:45 JST`：`backup_fileserver.py --check`（`restic check --read-data-subset=10%`）
- **監視連携**：`ubuntu-desktop` の Zabbix Trapper（`backup.fileserver.*` 5アイテム・失敗［Average］/ 28時間未成功［Warning］の 2トリガー）へ毎回送信し、異常時は Discord へ通知（[stacks/zabbix/configure_backup_monitoring.py](../stacks/zabbix/configure_backup_monitoring.py)）

### 復元手順（暫定ファイルサーバー `/mnt/files/important`）

```bash
python3 ~/dev/home-infra/stacks/backup/run_restic.py --fileserver snapshots --compact
python3 ~/dev/home-infra/stacks/backup/run_restic.py --fileserver restore latest --target /tmp/restore-fileserver
```

展開先の `/tmp/restore-fileserver/mnt/files/important/` から必要ファイルを戻し、作業後に `/tmp/restore-fileserver` を削除する。

### 初回リストア訓練の記録

- **テストスナップショット**：`<snapshot-id-drill>`（テストファイル `50 B`・所要 `11s`・Zabbix Trapper `5/5` 送信成功）
- **整合性・復元検証**：
  - `restic check --read-data`：全 2パック検証、`no errors were found`
  - `restic restore latest --target /tmp/restore-fileserver-drill`：復元ファイルと原本の SHA-256 が完全一致
- **訓練後クリーン状態**：テストファイル削除後にスナップショット `<snapshot-id-clean>` を取得（`0 B`・所要 `8s`・Zabbix Trapper `5/5` 送信成功）。B2 実保存容量は 2スナップショット計 `数 KiB`

## Git リポジトリ複製（Repolica・稼働中）

GitHub 単一障害点への備えとして、自作ツール [`example-user/example-repo-mirror`](https://github.com/example-user/example-repo-mirror) を `ubuntu-desktop` 上で非定常駐バッチ稼働させ、GitHub（`infra-admin`）から GitLab.com（`example-gitlab`）へ継続複製している（[stacks/repolica/](../stacks/repolica/)）。

- **実行方式**：`repolica:local`（Go 単一バイナリ＋`alpine:3.20`・`40.3 MB`・非 root `65532`・`--read-only`・`--cap-drop ALL`・`--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m`）を毎朝 `02:00 JST` に `cron`（`stacks/repolica/run_repolica.py`）から `sync --once`＋`status --json` で実行（常駐メモリ増 `0 MiB`）
- **対象範囲**：初期運用は `include: [example-private-repo, example-public-repo]`（private 1・public 1）で開始し、個人情報を含む別リポジトリは `exclude`＋GitHub Fine-grained PAT 権限分離の二重で除外（`stacks/repolica/repolica.yaml`）。ローカルの bare ミラーキャッシュは永続 Docker ボリューム `repolica-data` に保持
- **認証情報管理**：GitHub Fine-grained PAT（`Contents` / `Metadata` Read-only）と GitLab.com PAT（`api` / `write_repository`）を `stacks/repolica/.env`（`chmod 600`・Git 除外）に保管し、`backup_phase1_db.py` の `stacks/**/.env` 自動収集により B2 `infra-db` へ日次暗号化退避
- **監視**：`repolica.*` Zabbix Trapper 7 アイテム＋2 トリガー（同期失敗・28 時間未成功で Discord 通知）を登録・実測検証済み（初回 `2/2 healthy`・所要 `9s`・`processed: 7; failed: 0`）
- **セキュリティ設計**：`example-user/example-repo-mirror` 側をセルフ実行 `GIT_ASKPASS`（`/tmp` へ一時スクリプトを書かず `repolica` バイナリ自身が応答する方式）へ改修済みのため、`--read-only`＋`noexec` `/tmp` のまま稼働する

## Backblaze B2 の現況

- バケット名：`backup-home-infra-example`（Backblaze UI 上の作成日表記：`<creation-date>`）
- 公開設定：`Private`
- リージョン / S3 エンドポイント：`<B2_REGION>`（`s3.<B2_REGION>.backblazeb2.com`）
- サーバー側暗号化（Encryption）：`Enabled`
- Application Key：`backup-home-infra-example`（対象バケットを `backup-home-infra-example` 1バケットへ限定、`Read and Write`、`Allow List All Bucket Names` 無効）を発行済み
- リポジトリ：
  - `infra-db`（管理基盤 DB 7 本＋`.env` 11 個＋ホスト設定用・稼働中・実使用量 `約 30 MiB`、`repolica/.env` 追加）
  - `fileserver-important`（暫定ファイルサーバー `/mnt/files/important` 用・稼働中・実使用量 `数 KiB`）
- File Lifecycle：`Keep only the last version of the file`
- Object Lock：`Disabled`

## 未決事項

- モバイル端末写真の NAS 取り込み方法（UGOS の写真アプリか Immich か）
- 1Password 導入後の `restic` パスワード・B2 鍵の集約移行（当面は `chmod 600 .env` と自宅外控えで運用）
