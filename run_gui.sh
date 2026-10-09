#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_LIB="$PROJECT_DIR/.local/qt/usr/lib/x86_64-linux-gnu"
if [[ -d "$LOCAL_LIB" ]]; then
    export LD_LIBRARY_PATH="$LOCAL_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec "$PROJECT_DIR/.venv/bin/python" -m okx_gui "$@"
