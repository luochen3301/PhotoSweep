#!/bin/sh
set -eu

data_dir="${PHOTOSWEEP_DATA_DIR:-/app/data}"
cache_dir="${PHOTOSWEEP_CACHE_DIR:-/app/cache}"
trash_dir="${PHOTOSWEEP_TRASH_DIR:-/trash}"
puid="${PUID:-1000}"
pgid="${PGID:-1000}"

case "$puid" in
  '' | *[!0-9]*)
    echo "PUID 必须是非零正整数" >&2
    exit 64
    ;;
esac
case "$pgid" in
  '' | *[!0-9]*)
    echo "PGID 必须是非零正整数" >&2
    exit 64
    ;;
esac
if [ "$puid" -eq 0 ] || [ "$pgid" -eq 0 ]; then
  echo "PhotoSweep 不允许使用 root UID/GID 运行" >&2
  exit 64
fi

mkdir -p "$data_dir" "$cache_dir" "$trash_dir"

if [ "$(id -u)" = "0" ]; then
  chown -R "$puid:$pgid" "$data_dir" "$cache_dir" "$trash_dir"
  exec gosu "$puid:$pgid" "$@"
fi

exec "$@"
