#!/bin/sh
# Hugging Face Docker Space entry. Listens on $PORT (7860 on a Space). Optional bootstrap is
# for an empty database; a Space pointed at Neon should leave NWB_BOOTSTRAP unset.
set -eu
cd /home/user/app
mkdir -p var/logs var/blobs var/extractions var/cache

if [ -n "${NWB_BOOTSTRAP:-}" ]; then
  old_ifs=$IFS
  IFS=,
  # shellcheck disable=SC2086
  set -- $NWB_BOOTSTRAP
  IFS=$old_ifs
  for step in "$@"; do
    case "$step" in
      "" ) ;;
      migrate) python -m nwb migrate ;;
      layer1) python -m nwb load-layer1 ;;
      layer2) python -m nwb load-layer2 ;;
      *)
        echo "unknown NWB_BOOTSTRAP step: $step" >&2
        exit 2
        ;;
    esac
  done
fi

exec python -m nwb serve --host 0.0.0.0 --port "${PORT:-7860}"
