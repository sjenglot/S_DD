# S_DD — Shrink DD Rev 1.1.0

S_DD restores an existing whole-disk `.img` or `.img.gz` backup to a smaller compatible disk. Rev 1 supports exactly one primary NTFS partition on an MBR disk with 512-byte sectors. It uses an explicitly mounted scratch filesystem large enough for the complete decompressed source, a fitted image, and an allowance. The original backup is opened read-only; S_DD never formats scratch or deletes its work files.

**Experimental and destructive software.** A real Windows XP, NTFS, legacy BIOS/MBR installation was restored to a smaller Samsung SSD and booted successfully. S_DD prepared the fitted image, but the final write in that test was performed manually with GNU `dd` after an empty S_DD confirmation was rejected. The instrument software and hardware are not fully validated. The separate synthetic raw and gzip file-image integration test passed on CachyOS. Ubuntu remains untested. This project is provided as-is and is not actively maintained; users may fork, modify, and redistribute it under the license. Issues and pull requests are welcome, but responses or merges are not guaranteed.

## Commands

```bash
s_dd --help
s_dd --version
s_dd doctor
s_dd inspect IMAGE.img.gz
sudo s_dd restore IMAGE.img.gz /dev/disk/by-id/CONFIRMED-WHOLE-DISK --scratch /mounted/scratch --dry-run
sudo s_dd restore IMAGE.img.gz /dev/disk/by-id/CONFIRMED-WHOLE-DISK --scratch /mounted/scratch
sudo s_dd resume /mounted/scratch/sdd-JOB-DIRECTORY
```

The installed command is **`s_dd`**, because `sdd` may name another disk utility. `sdd.py` is the internal Python module. Running `s_dd` in a terminal opens the simple menu. `doctor` checks executables without opening a disk or appliance. `inspect` validates gzip integrity and parses the MBR and NTFS boot sector. `--dry-run` checks static layout, target identity, and scratch capacity; it does not measure the NTFS minimum. Restore validates and stages a scratch copy, measures and shrinks NTFS, fits the partition with `virt-resize --no-expand-content`, validates boot metadata, then saves `job.json` with a SHA-256 of the fitted image. An empty answer safely cancels and leaves the job; `s_dd resume JOB_DIRECTORY` revalidates the image and original destination identity before asking again. The write is verified by reading back the actual destination. Scratch and logs are retained.

Transfer stages show measured bytes, GiB, percentage, speed, and ETA when a total is known. Long filesystem operations show an elapsed-time spinner in an interactive terminal; utility output is saved in scratch. Redirected output uses occasional status lines instead of terminal control sequences. No percentage is invented for an opaque utility.

## Ubuntu installation

See [UBUNTU_FIRST_RUN.md](docs/UBUNTU_FIRST_RUN.md) for the complete, beginner-friendly workflow and [UBUNTU_QUICK_REFERENCE.md](docs/UBUNTU_QUICK_REFERENCE.md) for a printable summary. The Ubuntu 24.04 package set is `python3 python3-venv python3-pip python3-setuptools python3-wheel unzip util-linux fdisk ntfs-3g guestfish guestfs-tools libguestfs-tools qemu-utils`.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
.venv/bin/s_dd doctor
```

The rootless regular-file integration test is `python3 -B tests/integration_real_ntfs.py`. It uses only files in a private `.sdd-test-sandbox` case and must never be run with `sudo`. File-target restoration is available only with explicit `--test-file-target --test-case-dir` flags and refuses block devices, symlinks, and paths outside its private case. It is not the physical restore path.

For `sudo s_dd`, use the documented system installer, which installs a dedicated venv in `/opt/s_dd` and a launcher under `/usr/local/bin`. It refuses to overwrite an existing install. The user installer only makes `s_dd` available to the current account and does not guarantee sudo PATH access. Neither installer is run during development.

## Safety and limits

The physical destination must be a whole disk with a model and serial, with no mounted descendants, swap, or active holders. S_DD checks source and scratch ancestry, rechecks destination identity before and after confirmation, and refuses unsupported layouts. A mistaken confirmed disk can still be destroyed. An interruption after destination writing begins leaves it untrustworthy. The first Windows boot may run CHKDSK because NTFS resizing schedules a consistency check. Byte-level read-back and the synthetic file checks do not prove that a legacy Windows installation boots or that scientific instrument software works.

Only one primary NTFS partition on MBR with 512-byte sectors is supported. No other filesystem, OS, or multi-partition layout is validated. Successful file checks do not guarantee BIOS boot or instrument compatibility. The target disk is erased after explicit confirmation. Do not test on a system, source, or scratch disk. Read [ARCHITECTURE.md](docs/ARCHITECTURE.md), [HARDWARE_TEST.md](docs/HARDWARE_TEST.md), [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md), and [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) before hardware use. MIT licensed; review rights to contributed code before public release.
