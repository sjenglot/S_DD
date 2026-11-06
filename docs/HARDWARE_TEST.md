# Controlled physical SSD test checklist

Follow [UBUNTU_FIRST_RUN.md](UBUNTU_FIRST_RUN.md) for full installation, backup access, and scratch instructions. This checklist is for the expendable Kingston SSD on a separate Ubuntu work computer. The original FTIR HDD stays disconnected; the original backup stays unchanged.

1. Run `s_dd doctor`, the rootless libguestfs self-test, and the raw/gzip regular-file NTFS integration test on Ubuntu. Stop unless all pass.
2. Inventory with `lsblk -e7 -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,PTTYPE,MOUNTPOINTS` and `ls -l /dev/disk/by-id/`. Record Kingston model, serial, and whole-disk by-id path. Never select a `-partN` link or guess `/dev/sdX`.
3. Confirm the separate scratch filesystem is mounted, local ext4/XFS/Btrfs, not `/`, and has the **measured** free bytes reported by `findmnt` and `df -B1`. If the compressed source is copied onto scratch, count its size before the S_DD requirement.
4. Make `BACKUP`, `SCRATCH`, and `TARGET` point to verified resources. The source must be readable by `sudo`; a user GVFS path may not be. Run `.venv/bin/s_dd inspect "$BACKUP"` and stop on unexpected layout or gzip error.
5. Run `sudo s_dd restore "$BACKUP" "$TARGET" --scratch "$SCRATCH" --dry-run`. Record the actual target identity and required/available scratch bytes.
6. Run `sudo s_dd restore "$BACKUP" "$TARGET" --scratch "$SCRATCH"`. At `[y/N]`, enter `y` only after comparing model, serial, capacity and by-id path with the physical Kingston label. The destination is not written before image fitting and validation pass.
7. Require S_DD PASS. Save its `operations.log` and scratch image. Run `sync`, `lsblk -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,FSTYPE,PTTYPE,PARTTYPE,PARTFLAGS,MOUNTPOINTS "$TARGET"`, and `sudo sfdisk --dump "$TARGET"`. Compare the actual target with the expected one-primary-partition MBR layout.
8. Boot **only the expendable restored SSD** in the legacy instrument. Let Windows CHKDSK finish if requested. Verify Windows boot, files, licenses, instrument connection, and an actual measurement. A file checksum or byte-level PASS alone does not prove BIOS boot or instrument compatibility.

On blank/negative confirmation, S_DD retains the validated fitted image and prints a `resume` command; do not rerun the full 500 GB preparation. Resume only the same destination identity shown by the checkpoint. On interruption after destination writing begins, treat the SSD as incomplete. Preserve the original HDD, original backup, scratch directory, operation log, and displayed errors for diagnosis.
