# Contributing

S_DD is experimental destructive disk software and is not actively maintained. Forks, modifications, and redistribution are permitted under the included license. Issues and pull requests may be submitted, but responses and merges are not guaranteed.

Before proposing a change:

1. Run `python3 -B -m unittest discover -s tests -v` and `python3 -B -m py_compile sdd.py tests/test_cli.py`.
2. Keep physical-device writes out of automated tests. Use mocks or explicitly isolated disposable regular-file images.
3. Do not weaken destination identity, mounted-device, source-integrity, scratch-space, fitted-image, or read-back checks.
4. Describe what was tested and what remains untested. Never claim Windows bootability without a real boot test.

The currently validated scope is a legacy BIOS/MBR Windows XP installation with one NTFS partition, restored to a smaller SSD in the reported real-world test. Other layouts and filesystems are unsupported.
