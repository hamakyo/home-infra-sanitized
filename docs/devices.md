# 自宅デバイスの構成（サンプル）

状態： 運用中（サニタイズ済みサンプル構成。実機の固有型番・シリアル・個人識別子は一般化済み）

自宅基盤を構成する物理デバイスとネットワーク・IoT 機器のサンプル一覧。導入予定の機器は末尾に分けて記す。

## PC・モバイル

| デバイス | 種別 | 構成・OS（概要） | 主な役割 | 備考 |
|---|---|---|---|---|
| macOS ノート PC（`laptop-01.local`） | ノートPC | Apple Silicon / メモリ 8 GB / SSD 256 GB（FileVault 有効） / macOS | 日常作業、管理端末 | Fleet (`osquery`) 登録済み・Jamf Now 無料枠登録済み（Mac 用ブループリント・FileVault 個人復旧キー退避済み） |
| Windows デスクトップ（`workstation-01`） | デスクトップPC | 6C/6T CPU / メモリ 16 GB / ストレージ約 3 TB / Windows 11 Home / GPU 搭載 | 作業用・画像生成など（[backup-design.md](backup-design.md) 参照） | Fleet (`osquery`) 登録済み |
| Linux デスクトップ（`ubuntu-desktop`） | デスクトップPC | x86_64 4C/8T CPU / メモリ 12 GB（実効 11.6 GiB） / 内蔵 SATA SSD 240 GB ＋ 外付け USB SSD 480 GB（`ext4` `/mnt/files`） / Ubuntu 24.04 LTS | Phase 1 管理基盤・データ基盤ホスト（Docker 21 コンテナ稼働中）＋ 暫定ファイルサーバー（Samba SMB3：[fileserver.md](fileserver.md)） | Docker Engine + Compose・`osquery`・`zabbix-agent2` 稼働中。`multi-user.target` で GUI / RDP 可逆停止、`unattended-upgrades` 自動再起動（サンプル設定：`03:00 JST`）および Tailscale バインド耐性設定済み |
| iOS 端末（`mobile-01`） | スマートフォン | iOS | モバイル、写真撮影 | Jamf Now 無料枠登録済み（iOS 用ブループリント・非監視・セキュリティ適用済み） |

## ネットワーク・IoT

| デバイス | 種別 | 構成概要 | 主な役割 | 備考 |
|---|---|---|---|---|
| 上流ルーター（備付） | 上流ルーター / AP | NAT・DHCP `192.168.60.1`・上流 Wi-Fi 発信 | 宅内終端（二重 NAT 上流） | 上流有線 1000BASE-T → 既設ホームルーター WAN へ有線出力（[network.md](network.md) 参照） |
| 既設ホームルーター | ルーター | 家庭用 Wi-Fi ルーター（ルーターモード） | 自宅 LAN ゲートウェイ（`192.168.50.1`） | 接続構成は [network.md](network.md) 参照 |
| スマート電球（IoT） ×3 | スマート電球 | Wi-Fi 接続 IoT 機器 | 照明（IoT） | 将来 Yamaha 刷新時に専用 IoT VLAN へ分離予定 |

## 関連する別環境（参考）

- **外部 VM（`remote-vm-01`）**：自宅のローカル Linux デスクトップとは別の外部クラウド環境で稼働中（[backup-design.md](backup-design.md) 参照）

## 導入予定（未購入）

現況には含めない。購入・構築後に上の表へ移す。

- **NAS**：UGREEN NASync DXP4800 Plus + WD Red Plus 8TB ×4（選定理由は [NAS 選定の決定記録](decisions/0002-nas-ugreen-dxp4800-plus.md)）
- **ネットワーク刷新候補（将来構想・検討中）**：目標構成図は [target-architecture.md](target-architecture.md)、選定理由は [ネットワーク機器の選定記録](decisions/0003-network-yamaha-stack.md)
  - **L3 ルーター**：Yamaha RTX830
  - **L2 スイッチ**：Yamaha SWX2210-8G（ファンレス・非 PoE）
  - **無線 AP**：Yamaha WLX222（AC アダプタ `YPS-12HT` または PoE インジェクタ別途必要）
