#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: root 権限で実行してください (sudo bash $0)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISK_BY_ID="/dev/disk/by-id/usb-EXAMPLE_USB_SSD_0000000000000000-0:0"
MOUNT_POINT="/mnt/files"
FS_LABEL="home-files"
SMB_USER="infra-admin"
USB_VENDOR="XXXX"
USB_PRODUCT="YYYY"
FORCE_FORMAT=0
RESET_PASSWORD=0

for arg in "$@"; do
  case "$arg" in
    --force-format) FORCE_FORMAT=1 ;;
    --reset-password) RESET_PASSWORD=1 ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

echo "=== 1. 対象 USB SSD の安全確認 ==="
if [[ ! -L "$DISK_BY_ID" ]]; then
  echo "ERROR: 対象デバイスが見つかりません: $DISK_BY_ID" >&2
  exit 1
fi

DISK="$(readlink -f "$DISK_BY_ID")"
ROOT_SRC="$(findmnt -n -o SOURCE /)"
ROOT_DISK="/dev/$(lsblk -no PKNAME "$ROOT_SRC" | head -n1)"
DISK_TRAN="$(lsblk -no TRAN "$DISK" | head -n1)"

if [[ "$DISK" == "$ROOT_DISK" ]]; then
  echo "ERROR: ルートディスク ($ROOT_DISK) と一致したため中断します" >&2
  exit 1
fi
if [[ "$DISK_TRAN" != "usb" ]]; then
  echo "ERROR: 対象 ($DISK) の接続種別が usb ではありません (TRAN=$DISK_TRAN)" >&2
  exit 1
fi

echo "Target disk: $DISK ($DISK_BY_ID, TRAN=$DISK_TRAN)"
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT "$DISK"

echo "=== 2. パーティション作成・ext4 フォーマット ==="
PART="${DISK}1"
CURRENT_FSTYPE=""
CURRENT_LABEL=""
if [[ -b "$PART" ]]; then
  CURRENT_FSTYPE="$(blkid -s TYPE -o value "$PART" || true)"
  CURRENT_LABEL="$(blkid -s LABEL -o value "$PART" || true)"
fi

if [[ "$FORCE_FORMAT" -eq 1 || "$CURRENT_FSTYPE" != "ext4" || "$CURRENT_LABEL" != "$FS_LABEL" ]]; then
  echo "既存パーティションを消去し GPT + ext4 (label=$FS_LABEL) を作成します..."
  # Unmount any mounted partitions on this disk first
  while read -r mp; do
    if [[ -n "$mp" ]]; then
      echo "Unmounting $mp ..."
      umount "$mp"
    fi
  done < <(lsblk -ln -o MOUNTPOINT "$DISK")

  wipefs -a "$DISK"
  parted -s "$DISK" mklabel gpt
  parted -s -a optimal "$DISK" mkpart primary ext4 0% 100%
  partprobe "$DISK"
  udevadm settle

  if [[ ! -b "$PART" ]]; then
    echo "ERROR: パーティション $PART が認識されませんでした" >&2
    exit 1
  fi

  mkfs.ext4 -F -m 1 -L "$FS_LABEL" "$PART"
  udevadm settle
else
  echo "既に ext4 (label=$FS_LABEL) で初期化済みのためフォーマットをスキップします。"
fi

UUID="$(blkid -s UUID -o value "$PART")"
if [[ -z "$UUID" ]]; then
  echo "ERROR: UUID を取得できませんでした ($PART)" >&2
  exit 1
fi
echo "Partition: $PART (UUID=$UUID)"

echo "=== 3. /etc/fstab 登録と /mnt/files マウント ==="
mkdir -p "$MOUNT_POINT"
if ! mountpoint -q "$MOUNT_POINT"; then
  # 未マウント時に OS SSD 側へ誤書き込みされないようマウントポイントを read-only 化
  chown root:root "$MOUNT_POINT"
  chmod 0555 "$MOUNT_POINT"
fi
FSTAB_LINE="UUID=${UUID} ${MOUNT_POINT} ext4 defaults,noatime,nofail,x-systemd.device-timeout=10 0 2"

if grep -qE "[[:space:]]${MOUNT_POINT}[[:space:]]" /etc/fstab; then
  sed -i "\|[[:space:]]${MOUNT_POINT}[[:space:]]|c\\${FSTAB_LINE}" /etc/fstab
else
  printf '\n# 外付け USB SSD 480 GB temporary file server\n%s\n' "$FSTAB_LINE" >> /etc/fstab
fi

systemctl daemon-reload
if ! mountpoint -q "$MOUNT_POINT"; then
  mount "$MOUNT_POINT"
fi
if ! mountpoint -q "$MOUNT_POINT"; then
  echo "ERROR: $MOUNT_POINT のマウントに失敗しました" >&2
  exit 1
fi

mkdir -p "${MOUNT_POINT}/share" "${MOUNT_POINT}/important"
chown "${SMB_USER}:${SMB_USER}" "$MOUNT_POINT" "${MOUNT_POINT}/share" "${MOUNT_POINT}/important"
chmod 0750 "$MOUNT_POINT" "${MOUNT_POINT}/share" "${MOUNT_POINT}/important"

echo "=== 4. USB 安定化設定 (UAS 無効化・autosuspend 無効化・max_sectors_kb=128) ==="
cat > /etc/modprobe.d/usb-ssd-disable-uas.conf <<EOF
# 外付け USB SSD 480 GB (${USB_VENDOR}:${USB_PRODUCT}): disable UAS to prevent write-load timeouts
options usb-storage quirks=${USB_VENDOR}:${USB_PRODUCT}:u
EOF
chmod 0644 /etc/modprobe.d/usb-ssd-disable-uas.conf

cat > /etc/udev/rules.d/99-usb-ssd-no-autosuspend.rules <<EOF
ACTION=="add|change", SUBSYSTEM=="usb", ATTR{idVendor}=="${USB_VENDOR}", ATTR{idProduct}=="${USB_PRODUCT}", TEST=="power/control", ATTR{power/control}="on"
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd[a-z]", ATTRS{idVendor}=="${USB_VENDOR}", ATTRS{idProduct}=="${USB_PRODUCT}", ATTR{queue/max_sectors_kb}="128"
EOF
udevadm control --reload-rules

DISK_NAME="$(basename "$DISK")"
if [[ -w "/sys/block/${DISK_NAME}/queue/max_sectors_kb" ]]; then
  echo 128 > "/sys/block/${DISK_NAME}/queue/max_sectors_kb"
fi

for devdir in /sys/bus/usb/devices/*; do
  if [[ -f "${devdir}/idVendor" && -f "${devdir}/idProduct" ]]; then
    if [[ "$(cat "${devdir}/idVendor")" == "$USB_VENDOR" && "$(cat "${devdir}/idProduct")" == "$USB_PRODUCT" ]]; then
      echo on > "${devdir}/power/control"
      echo "Applied power/control=on to ${devdir}"
    fi
  fi
done

echo "=== 5. Samba 導入・設定 ==="
if ! dpkg -s samba >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y samba
fi

if [[ ! -f /etc/samba/smb.conf.orig && -f /etc/samba/smb.conf ]]; then
  cp -a /etc/samba/smb.conf /etc/samba/smb.conf.orig
fi
install -m 0644 "${SCRIPT_DIR}/smb.conf" /etc/samba/smb.conf
testparm -s >/dev/null

mkdir -p /etc/systemd/system/smbd.service.d
cat > /etc/systemd/system/smbd.service.d/10-after-tailscaled.conf <<'EOF'
[Unit]
After=network-online.target tailscaled.service mnt-files.mount
Wants=network-online.target tailscaled.service

[Service]
ExecStartPre=/bin/sh -c 'for i in $(seq 1 15); do ip -4 addr show dev tailscale0 2>/dev/null | grep -q "inet " && exit 0; sleep 1; done; exit 0'
EOF

systemctl daemon-reload
systemctl disable --now nmbd.service 2>/dev/null || true
systemctl enable --now smbd.service
systemctl restart smbd.service

echo "=== 6. Avahi SMB 広告設定 (LAN 検出用) ==="
if [[ -d /etc/avahi/services ]]; then
  install -m 0644 "${SCRIPT_DIR}/samba.avahi.service" /etc/avahi/services/samba.service
  systemctl reload-or-restart avahi-daemon.service || true
fi

echo "=== 7. UFW ファイアウォール設定 (445/tcp を LAN・Tailscale のみ許可) ==="
ufw allow in on eno1 from 192.168.50.0/24 to any port 445 proto tcp comment 'Samba LAN'
ufw allow in on tailscale0 to any port 445 proto tcp comment 'Samba Tailscale'

echo "=== 8. Samba ユーザー (${SMB_USER}) パスワード設定 ==="
if [[ "$RESET_PASSWORD" -eq 1 ]] || ! pdbedit -L | grep -q "^${SMB_USER}:"; then
  echo "Samba 接続用のパスワードを設定してください (ユーザー: ${SMB_USER})"
  smbpasswd -a "$SMB_USER"
  smbpasswd -e "$SMB_USER"
else
  echo "Samba ユーザー ${SMB_USER} は登録済みです (変更時は --reset-password を付けて実行)"
fi

echo "=== 9. 構築結果の検証 ==="
df -h "$MOUNT_POINT"
echo "--- Mount options ---"
findmnt "$MOUNT_POINT"
echo "--- Listening SMB ports ---"
ss -tlnp | grep ':445 ' || true
echo "--- UFW ---"
ufw status numbered
echo "--- SMART check (best-effort) ---"
smartctl -i -H "$DISK" || true
echo "=== 完了 ==="
