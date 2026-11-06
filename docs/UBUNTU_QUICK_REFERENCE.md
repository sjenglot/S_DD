# S_DD Rev 1.1.0 — Ubuntu 24.04 quick reference

**Controlled test only. Original FTIR HDD disconnected. Target Kingston SSD expendable. Never guess a `/dev/sdX` name.** Read [UBUNTU_FIRST_RUN.md](UBUNTU_FIRST_RUN.md) before using these commands.

## Install from the ZIP

```bash
unzip S_DD-v1.1.0-source.zip
cd S_DD-v1.1.0
sudo apt update
sudo apt install python3 python3-venv python3-pip python3-setuptools python3-wheel unzip util-linux fdisk ntfs-3g guestfish guestfs-tools libguestfs-tools qemu-utils
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
.venv/bin/s_dd doctor
sudo bash scripts/install_system.sh
s_dd --version
sudo s_dd --help
mkdir -m 700 .sdd-test-sandbox
python3 -B tests/libguestfs_selftest.py
python3 -B tests/integration_real_ntfs.py
```

The integration test must report PASS for **raw and gzip** and all four file SHA-256 checks. Ubuntu execution is still pending; stop if it fails.

## Identify, inspect, dry run

```bash
lsblk -e7 -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,PTTYPE,MOUNTPOINTS
ls -l /dev/disk/by-id/
BACKUP='/path/to/Nicolet_FTIR_2026-11-03.img.gz'
SCRATCH='/mnt/REPLACE-WITH-MOUNTED-SCRATCH-DIRECTORY'
TARGET='/dev/disk/by-id/REPLACE-WITH-EXACT-KINGSTON-WHOLE-DISK-ID'
findmnt -T "$SCRATCH" -o TARGET,SOURCE,FSTYPE,OPTIONS,AVAIL
df -B1 "$SCRATCH"
lsblk -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,MOUNTPOINTS "$TARGET"
sudo test -r "$BACKUP"
.venv/bin/s_dd inspect "$BACKUP"
sudo s_dd restore "$BACKUP" "$TARGET" --scratch "$SCRATCH" --dry-run
```

If the network share is only available through user GVFS, copy the compressed backup as your normal user to a new file on scratch, compare SHA-256 hashes, and set `BACKUP` to that copy. Account for its compressed size **in addition to** S_DD's reported scratch requirement. Require local ext4, XFS, or Btrfs scratch, mounted separately from `/`. A nominal 1 TB drive is not a space guarantee.

## Destructive restore and independent checks

```bash
sudo s_dd restore "$BACKUP" "$TARGET" --scratch "$SCRATCH"
sync
lsblk -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,FSTYPE,PTTYPE,PARTTYPE,PARTFLAGS,MOUNTPOINTS "$TARGET"
sudo sfdisk --dump "$TARGET"
```

At the S_DD prompt, check the **model, serial, capacity and by-id path** against the physical Kingston label. Enter `y` only if all match. Blank or `n` cancels and preserves the fitted image. To continue later, use the printed command, for example `sudo s_dd resume "$SCRATCH/sdd-JOB-DIRECTORY"`; the same recorded disk identity is mandatory. Destination writing begins only after fitted-image checks and confirmation. Preserve `operations.log`. Let Windows CHKDSK finish on first boot. File checks and read-back do not guarantee BIOS boot or instrument operation.
