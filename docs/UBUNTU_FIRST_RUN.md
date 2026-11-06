# S_DD Rev 1.1.0: first Ubuntu work-computer run

This is a **controlled test release**, reviewed for Ubuntu 24.04 LTS but not yet run on Ubuntu or a physical SSD. Use only the expendable Kingston SSD as the destination. Keep the original Seagate HDD disconnected. Never modify the original compressed backup. Stop when any identity, size, mount, filesystem, or utility result differs from expectations.

Commands below use Bash on Ubuntu. Text such as `REPLACE-WITH-CONFIRMED-ID` is a placeholder, **not** a device to use as written.

## 1. Receive the ZIP and install software

Copy `S_DD-v1.1.0-source.zip` to a local Ubuntu folder. Inspect its members, then extract it:

```bash
unzip -l S_DD-v1.1.0-source.zip
unzip S_DD-v1.1.0-source.zip
cd S_DD-v1.1.0
```

Install the Ubuntu 24.04 packages. `guestfish`, `guestfs-tools`, and `libguestfs-tools` are in Ubuntu's Universe repository; have the work-computer administrator enable that repository if apt cannot locate them.

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip python3-setuptools python3-wheel unzip util-linux fdisk ntfs-3g guestfish guestfs-tools libguestfs-tools qemu-utils
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
.venv/bin/s_dd --help
.venv/bin/s_dd doctor
sudo bash scripts/install_system.sh
s_dd --version
sudo s_dd --help
```

The system installer creates a private Python venv under `/opt/s_dd` and `/usr/local/bin/s_dd`. It refuses to replace an existing install. `/usr/local/bin` is normally present in sudo's secure path, so `sudo s_dd` does not depend on inheriting your personal PATH. The project-local `.venv/bin/s_dd` remains available for normal-user checks. `doctor` checks command availability without launching an appliance or touching a disk. The separate file-based integration test checks actual libguestfs/virt-resize NTFS support. The installed command is **`s_dd`**, not `sdd`.

Before a physical restore, run the included rootless regular-file test from this extracted project directory:

```bash
mkdir -m 700 .sdd-test-sandbox
python3 -B tests/libguestfs_selftest.py
python3 -B tests/integration_real_ntfs.py
```

The second command must end with `PASS: real NTFS raw and gzip restore, all file SHA-256 checks matched.` and create a `results.json` inside a new `.sdd-test-sandbox/real-ntfs-*` case. It uses only disposable regular files. If either command fails, stop. Ubuntu success is not assumed from the earlier CachyOS run.

## 2. Identify all physical disks before setting paths

Connect the expendable Kingston SSD and the separate 1 TB scratch disk. Leave the original HDD disconnected. Run:

```bash
lsblk -e7 -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,PTTYPE,MOUNTPOINTS
ls -l /dev/disk/by-id/
```

Match **model, serial, and capacity** to the labels on the Kingston and scratch drives. The Kingston target must be a whole-disk by-id link without a `-partN` suffix. Never infer safety from `/dev/sda`, `/dev/sdb`, or `/dev/sdc`. Do not use the Ubuntu system disk, the scratch disk, or the disk holding the source backup as the target.

The scratch disk must be mounted already on a local ext4, XFS, or Btrfs filesystem. S_DD will not format or mount it. Example placeholder:

```bash
SCRATCH='/mnt/REPLACE-WITH-MOUNTED-SCRATCH-DIRECTORY'
findmnt -T "$SCRATCH" -o TARGET,SOURCE,FSTYPE,OPTIONS,AVAIL
df -B1 "$SCRATCH"
```

If it is not mounted, ask the work-computer administrator to mount the **identified scratch filesystem**. Do not guess a partition or run formatting commands. The mount must differ from `/` and have enough measured free space.

## 3. Make the network backup accessible without changing it

The backup `Nicolet_FTIR_2026-11-03.img.gz` is on the University of Calgary Science share. A desktop GVFS path under `/run/user/.../gvfs` may be inaccessible to `sudo`. Two safe choices are:

1. Ask IT to provide a system-mounted, root-readable, preferably read-only SMB mount. Confirm `sudo test -r "$BACKUP"` succeeds. Keep credentials out of shell history and S_DD arguments.
2. As your normal user, copy the compressed backup **once** from the user-accessible share to a new file on the scratch drive. Verify its SHA-256 against the share copy, then use the local copy as `BACKUP`. The original on the share remains untouched.

For the copy option, fill in the share path only after locating it. Check that the destination filename does not already exist:

```bash
SHARE_BACKUP='/path/to/network-share/Nicolet_FTIR_2026-11-03.img.gz'
LOCAL_BACKUP="$SCRATCH/Nicolet_FTIR_2026-11-03.img.gz"
test -r "$SHARE_BACKUP"
test ! -e "$LOCAL_BACKUP"
cp -- "$SHARE_BACKUP" "$LOCAL_BACKUP"
sha256sum "$SHARE_BACKUP" "$LOCAL_BACKUP"
chmod a-w "$LOCAL_BACKUP"
BACKUP="$LOCAL_BACKUP"
sudo test -r "$BACKUP"
```

The two printed SHA-256 values must match. Copying to scratch consumes **additional** space equal to the compressed file size before S_DD's two-image scratch requirement. Do not choose this option unless the free space **after the copy** still exceeds S_DD's reported requirement. If the share is too slow or unreliable, stop and arrange a verified local copy on another disk.

## 4. Inspect the backup and calculate scratch capacity

Set the target only after matching its physical label and the `lsblk` inventory:

```bash
TARGET='/dev/disk/by-id/REPLACE-WITH-EXACT-KINGSTON-WHOLE-DISK-ID'
readlink -f "$TARGET"
lsblk -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,MOUNTPOINTS "$TARGET"
stat -c '%n %s compressed bytes' "$BACKUP"
.venv/bin/s_dd inspect "$BACKUP"
```

`inspect` reads all of the gzip stream, checks gzip integrity, and reports the **uncompressed** whole-disk size and MBR/NTFS layout. It must show exactly one primary NTFS partition. Record the measured size. S_DD's scratch requirement is:

`uncompressed source bytes + fitted image bytes + max(2 GiB, 5% of those two images)`.

For illustration only, a 500 GB source and approximately 255 GB fitted image would need roughly **793 GB** of scratch free space, before the compressed backup copy and any other files. The actual capacity and free-space gate come from S_DD and `df`, not this illustration. A nominal 1 TB scratch SSD is therefore **not automatically sufficient**.

## 5. Dry run, then controlled restore

From the extracted release directory:

```bash
sudo s_dd restore "$BACKUP" "$TARGET" --scratch "$SCRATCH" --dry-run
```

The dry run checks image structure, target identity and capacity, mount safety, and scratch free space. It **does not** measure the NTFS minimum or prove the shrink will work. Stop on any refusal or unexpected target identity.

When ready, run the actual operation with an attached terminal so you can read the confirmation prompt:

```bash
sudo s_dd restore "$BACKUP" "$TARGET" --scratch "$SCRATCH"
```

S_DD stages and shrinks a scratch copy and validates the smaller image **before** asking for destructive confirmation. At the prompt, compare the displayed Kingston model, serial, capacity, and by-id path with your notes. Enter `y` only if every field matches. Empty input, `n`, EOF, or Ctrl+C cancels before writing. S_DD retains the fitted image and prints a resume command. If interrupted after writing begins, treat the SSD as incomplete. S_DD retains its `sdd-*` job directory, checkpoint, and `operations.log`; do not delete them until validation is finished.

To resume after a safe cancellation, run the exact command printed by S_DD, normally:

```bash
sudo s_dd resume "$SCRATCH/sdd-JOB-DIRECTORY"
```

Resume verifies the fitted image checksum and layout, then requires the same destination path, model, serial, and capacity recorded in the job. It repeats mounted-disk and source/scratch overlap checks and asks for a new `y/N` confirmation. It does not repeat decompression or NTFS shrinking.

## 6. Verify and boot the expendable SSD

S_DD compares the written image bytes against the actual SSD and checks the destination MBR and NTFS metadata. Independently inspect the target again:

```bash
sync
lsblk -b -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,FSTYPE,PTTYPE,PARTTYPE,PARTFLAGS,MOUNTPOINTS "$TARGET"
sudo sfdisk --dump "$TARGET"
```

Find the actual child partition from `lsblk`; do not assume a by-id `-part1` link exists. Do not mount or boot an SSD that S_DD marked FAIL. A successful read-back and the earlier synthetic file checks do **not** prove that the legacy Windows installation will boot or the instrument will work.

Only after offline checks pass, connect **the expendable restored SSD** to the legacy instrument computer. Keep the original HDD untouched and disconnected. Windows may run CHKDSK because `ntfsresize` intentionally schedules a consistency check. Let it finish; do not interrupt it. Then verify Windows boot, instrument software, license state, communications, and an actual measurement. Preserve the original backup, the original HDD, and the scratch logs regardless of outcome.
