#!/usr/bin/env python3
"""Compatibility shim for the old experimental command."""
import sys
import sdd

if __name__ == '__main__':
    print('sdd-shrink.py is retired. Use: s_dd restore IMAGE DISK --scratch DIR [--dry-run]', file=sys.stderr)
    sys.exit(2)
