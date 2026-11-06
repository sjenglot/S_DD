# Changelog

## 1.1.0 — 2026-11-05

- Fixed `lsblk` whole-disk detection using tree output and `PKNAME` ancestry, independent of JSON row order.
- Replaced the typed erase phrase with a repeatable yes/no prompt; blank, negative, EOF, and Ctrl+C cancel safely.
- Added a SHA-256 checked fitted-image checkpoint and `s_dd resume JOB_DIRECTORY`; the resume path revalidates the original destination identity and safety conditions.
- Added a two-choice interactive terminal menu when `s_dd` starts without arguments.
- Added user and system installation scripts; the system launcher is placed in `/usr/local/bin` for ordinary sudo secure paths and refuses to replace existing installs.
- Preserved the Rev 1 scratch-first NTFS/MBR fitting workflow.

## 1.0.0 test release

- Initial scratch-assisted restore workflow for a single MBR NTFS primary partition.
