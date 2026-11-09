#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=
need_python=false
for arg in "$@"; do
    if [ "$need_python" = true ]; then python=$arg; need_python=false; continue; fi
    case "$arg" in
        --python) need_python=true ;;
        --python=*) python=${arg#--python=} ;;
    esac
done
if [ "$need_python" = true ]; then echo '--python requires an executable' >&2; exit 2; fi
if [ -z "$python" ]; then
    if command -v python3 >/dev/null 2>&1; then python=python3; else python=python; fi
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$python" -B "$script_dir/entry.py" "$@"
