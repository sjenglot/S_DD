#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
install_root="${HOME}/.local/opt/s_dd"
command_path="${HOME}/.local/bin/s_dd"

if [[ -e "${install_root}" || -L "${install_root}" || -e "${command_path}" || -L "${command_path}" ]]; then
    echo "Refusing to overwrite an existing S_DD installation: ${install_root} or ${command_path}" >&2
    exit 1
fi
command -v python3 >/dev/null || { echo 'Install Python 3 first.' >&2; exit 1; }
python3 -m venv --system-site-packages "${install_root}/venv"
"${install_root}/venv/bin/python" -m pip install --no-cache-dir --no-build-isolation --no-deps "${project_dir}"
mkdir -p "${HOME}/.local/bin"
ln -s "${install_root}/venv/bin/s_dd" "${command_path}"
echo "Installed ${command_path}"
echo 'Ensure ~/.local/bin is already on PATH, or invoke the command by its full path.'
