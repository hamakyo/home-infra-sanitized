# 暫定ファイルサーバーの構成（ubuntu-desktop + USB SSD）

状態： 運用中（実測検証済み）

本番 NAS（`UGREEN NASync DXP4800 Plus`・未購入）を導入するまでのつなぎとして、`ubuntu-desktop` に手持ちのポータブル SSD を直結して構築した暫定ファイルサーバーの構成と運用ルール。

## 位置づけと利用範囲

- **役割**：macOS ノート PC・`workstation-01`・iOS 端末 間でのファイル受け渡し、軽量な作業データ置き場、本番 NAS 導入前の SMB / Tailscale 運用の先行検証
- **対象外**：単体 USB SSD（実効 440 GB・冗長性なし）のため、Time Machine の世代保管、大量の動画・生成画像アーカイブ、バックアップなしの単一正本保管には使わない

## ストレージ・マウント構成（実測確認済み）

| 項目 | 確認値・設定 |
|---|---|
| 物理ドライブ | `外付け USB SSD 480 GB`（480 GB / USB ID `XXXX:YYYY` / USB 3.1 Gen1 `5000M`・要求電流 `896mA`・`usb-storage`［BOT］ドライバ。内蔵 SATA SSD は SATA-USB ブリッジ ファームウェア正常、SMART `PASSED`・寿命残 `99%`・エラー `0`・`33℃` を実測確認） |
| 接続先 | `ubuntu-desktop` USB 3.0 ポート直結（`usb 3-1`・`5000M`・`/dev/disk/by-id/usb-EXAMPLE_USB_SSD_0000000000000000-0:0`、udev により `max_sectors_kb=128` 自動適用を確認） |
| ファイルシステム | GPT 単一パーティション・`ext4`（ラベル `home-files`・予約領域 `-m 1`・実効 `440G` / 空き `435G`） |
| マウント設定 | `/etc/fstab` に UUID 指定（`defaults,noatime,nofail,x-systemd.device-timeout=10`）で `/mnt/files` へ固定マウント |
| 自動再起動・抜け対策 | `nofail` により毎日 `03:00 JST` の `unattended-upgrades` 自動再起動時に SSD が抜けていても起動停止しない。さらに未マウント状態の `/mnt/files` を `root:root 0555` に設定し、OS SSD（`/dev/sda2`）への誤書き込みを防止 |
| USB 安定化・省電力対策 | `/etc/modprobe.d/usb-ssd-disable-uas.conf`（`options usb-storage quirks=XXXX:YYYY:u`＋`update-initramfs -u`）で UASP（`uas`）を無効化し、`/etc/udev/rules.d/99-usb-ssd-no-autosuspend.rules` で `XXXX:YYYY` の USB autosuspend 無効化（`power/control=on`）と最大転送長制限（`queue/max_sectors_kb=128`）を自動適用。64 MiB `dd conv=fdatasync` 負荷試験でエラー 0 を実測確認済み |

### ディレクトリ分離

- `/mnt/files/share`：通常共有・端末間の一時受け渡し用（B2 課金肥大を防ぐためバックアップ対象外）
- `/mnt/files/important`：重要書類などの保管用（`restic` → Backblaze B2 `backup-home-infra-example/fileserver-important` へ毎朝 `01:45 JST` 日次暗号化バックアップ稼働中・リストア訓練済み）
- `/mnt/files/lost+found`：`ext4` 標準のシステム修復用ディレクトリ（`root` 専用のため `veto files = /lost+found/` で SMB クライアントから非表示）

## 共有サービス（Samba / SMB3）とセキュリティ設計

構成ファイル・導入スクリプトは [stacks/fileserver/](../stacks/fileserver/) で管理する。

| 項目 | 設定 |
|---|---|
| ソフトウェア | `smbd 4.19.5-Ubuntu`（ホストネイティブ稼働・`nmbd` 無効・TCP `445` のみ） |
| プロトコル・暗号化 | `server min protocol = SMB3`・`smb encrypt = desired`・`server multi channel support = no`（到達不能な Docker ブリッジ `172.x.0.1` の誤広告による macOS Finder 遅延を防止） |
| 認証 | `map to guest = never`（ゲスト完全禁止）・`valid users = infra-admin`（Ubuntu ログインとは別の Samba 専用パスワードを設定。保管場所：手元控え、将来 1Password へ集約） |
| ネットワーク隔離 | `tailscale0` が `/32` の `POINTOPOINT` TUN インターフェースであり Samba の `bind interfaces only = yes` でスキップされる仕様のため、`bind interfaces only = no` とし、**ホスト UFW**（`eno1` の `192.168.50.0/24` と `tailscale0` のみ `445/tcp` 許可）＋ **Samba `hosts allow`**（`127.0.0.1`・`::1`・`192.168.50.0/24`・`100.64.0.0/10`・`fd7a:115c:a1e0::/48`、`hosts deny = ALL`）の二重防御を適用。Docker コンテナ（`caddy` から `172.18.0.1:445`）からの到達遮断を実測確認済み |
| macOS / iOS 対応 | `vfs objects = fruit streams_xattr`（`fruit:metadata = stream`）＋ Avahi `_smb._tcp` 広告（`/etc/avahi/services/samba.service`） |
| 起動順序 | `/etc/systemd/system/smbd.service.d/10-after-tailscaled.conf` で `network-online.target`・`tailscaled.service`・`mnt-files.mount` の後に起動 |

## 各端末からの接続方法

共有名はいずれも `files`、ユーザー名は `infra-admin`（設定した Samba 専用パスワードを使用）。

| 端末 | 自宅 LAN（高速・優先） | Tailscale（自宅外含む） |
|---|---|---|
| macOS ノート PC（Finder → `Cmd+K`） | `smb://192.168.50.4/files`（または `smb://ubuntu-desktop.local/files`） | `smb://100.64.0.10/files` |
| Windows `workstation-01`（エクスプローラー） | `\\192.168.50.4\files` | `\\100.64.0.10\files` |
| iOS 端末（「ファイル」→ サーバへ接続） | `smb://192.168.50.4`（認証後に `files` を選択） | `smb://100.64.0.10`（認証後に `files` を選択） |

## 監視・バックアップと運用

- **Backblaze B2 日次バックアップ（稼働中）**：`/mnt/files/important` のみを対象に [stacks/backup/backup_fileserver.py](../stacks/backup/backup_fileserver.py) が毎朝 `01:45 JST` に `restic` 暗号化退避（`backup-home-infra-example/fileserver-important`、保持 `日次7 / 週次4 / 月次12 --prune`、毎月 1 日 `02:45 JST` に `--check` 実行）。`/mnt/files` が未マウントの場合は空ディレクトリでの誤上書きを防いで即座に失敗扱いとし、Zabbix Trapper（`backup.fileserver.*` 5 項目・失敗 / 28 時間未成功の 2 トリガー）から Discord へ通知する。リストア訓練結果と詳細手順は [docs/backup-design.md](backup-design.md) を参照
  ```bash
  python3 ~/dev/home-infra/stacks/backup/run_restic.py --fileserver snapshots --compact
  python3 ~/dev/home-infra/stacks/backup/run_restic.py --fileserver restore latest --target /tmp/restore-fileserver
  ```
- **Zabbix 監視**：`ubuntu-desktop` のネイティブ `zabbix-agent2` が USB SSD のディスク I/O（`vfs.dev.*`）および `/mnt/files` の容量・inode（`vfs.fs.get[short]` LLD）を自動検出し、`backup.fileserver.*` 5 項目で B2 退避の成否と鮮度を監視する
- **再現・再適用手順**：
  ```bash
  sudo bash ~/dev/home-infra/stacks/fileserver/setup_host.sh
  ```
  冪等設計のため、既に `ext4`（ラベル `home-files`）かつユーザー登録済みの状態ではフォーマットとパスワード設定をスキップし、設定ファイル・USB quirk・udev・UFW のみ再適用する（パスワード変更時は `--reset-password` を付与）
- **大容量転送の制約と実測障害記録**：
  - `外付け USB SSD 480 GB`（`XXXX:YYYY`・バスパワー要求 `bMaxPower = 896mA`）に対し、UAS 無効化・背面 USB 3.0 ポート `usb 3-2` 接続後でも Mac から SMB 経由で 30 GB 級の一括コピーを行うと `CDB: Synchronize Cache(10)` が 267 秒無応答→USB リセット 5 回失敗→カーネルにより `Device offlined`・`ext4` ジャーナル停止（全 I/O が `-5` エラー）となる事象を確認した
  - さらに追加切り分けとして、Samba を介さない SSH 経由 `rsync --bwlimit=10000`（約 10 MB/s 制限・小バッチ分割）で連続書き込み試験を実施したところ、USB 3.0（`usb 3-2`・`max_sectors_kb=128`）では `0.78 GiB` 時点、USB 2.0（`usb 1-3`・`480M`・`500mA`・`max_sectors_kb=120`）でも `1.56 GiB` 時点でそれぞれ `xhci_hcd` の USB デバイスリセットが再現した（いずれも監視スクリプトで即停止し `ext4` 破損なし）
  - **運用ルール**：本 USB SSD はバスパワー直結／USB-SATA ブリッジ側の制約により GB 級の連続書き込みには耐えられない。小容量ファイル（数十〜数百 MB 程度）の受け渡し・軽量保管に限定し、GB 級データ（数十 GB 級の個人データ等）の本格移行は本番 NAS（Phase 3）導入まで保留する
  - **udev ルール修正**：`block` サブシステムの `/dev/sdX` には `ENV{ID_VENDOR_ID}` が付与されないため、`99-usb-ssd-no-autosuspend.rules` の一致条件を `KERNEL=="sd[a-z]", ATTRS{idVendor}=="XXXX", ATTRS{idProduct}=="YYYY"` へ修正し、再接続・再起動後も `queue/max_sectors_kb=128` が自動適用されるようにした
- **将来の拡張余地**：ブラウザ操作が必要になれば軽量な `FileBrowser`（`https://files.home.example.com`＋Authentik Forward Auth）を追加可能
