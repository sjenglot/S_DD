#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo 'Run this installer with sudo: sudo bash scripts/install_system.sh' >&2
    exit 1
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
install_root='/opt/s_dd'
command_path='/usr/local/bin/s_dd'

if [[ -e "${install_root}" || -L "${install_root}" || -e "${command_path}" || -L "${command_path}" ]]; then
    echo "Refusing to overwrite an existing installation at ${install_root} or ${command_path}." >&2
    exit 1
fi
command -v python3 >/dev/null || { echo 'Install Python 3 first.' >&2; exit 1; }
python3 -m venv --system-site-packages "${install_root}/venv"
"${install_root}/venv/bin/python" -m pip install --no-cache-dir --no-build-isolation --no-deps "${project_dir}"
ln -s "${install_root}/venv/bin/s_dd" "${command_path}"
echo "Installed ${command_path}; it is available to sudo through the usual /usr/local/bin secure path."
