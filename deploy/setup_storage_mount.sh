#!/usr/bin/env bash
# ==============================================================================
# setup_storage_mount.sh
# Permanently mounts the external media storage drive by UUID in /etc/fstab
# This prevents device-renaming issues (/dev/sdb -> /dev/sdc) when drives disconnect.
# ==============================================================================

set -euo pipefail

MOUNT_DIR="/mnt/sdb3"
# The UUID of your external media storage partition (detected from blkid)
DEFAULT_UUID="ab6159e2-f6de-4112-a363-c246aa70c6d9"

UUID="${1:-$DEFAULT_UUID}"

echo ">>> Configuring persistent mount for UUID=${UUID} at ${MOUNT_DIR}..."

# Ensure target mount directory exists
sudo mkdir -p "${MOUNT_DIR}"

# Check if entry already exists in /etc/fstab
if grep -q "${UUID}" /etc/fstab; then
    echo ">>> /etc/fstab already contains an entry for UUID=${UUID}."
else
    echo ">>> Adding persistent entry to /etc/fstab with 'nofail' guard..."
    FSTAB_LINE="UUID=${UUID} ${MOUNT_DIR} ext4 defaults,nofail,noatime 0 2"
    echo "${FSTAB_LINE}" | sudo tee -a /etc/fstab
    echo ">>> Added: ${FSTAB_LINE}"
fi

# Test mounting
echo ">>> Testing mount..."
sudo mount -a

if mountpoint -q "${MOUNT_DIR}"; then
    echo ">>> SUCCESS: ${MOUNT_DIR} is cleanly mounted!"
    sudo chown -R user:user "${MOUNT_DIR}" 2>/dev/null || true
    sudo chmod 777 "${MOUNT_DIR}/incoming" 2>/dev/null || true
    df -h "${MOUNT_DIR}"
else
    echo ">>> WARNING: ${MOUNT_DIR} is not mounted. Check if device is plugged in."
fi

