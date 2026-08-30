#!/usr/bin/env bash
#
# Move an ElectrumX instance's database aside so it resyncs from genesis.
#
#   contrib/rtm_reindex.sh <instance> [--cache MB]
#   contrib/rtm_reindex.sh <instance> --rollback
#   contrib/rtm_reindex.sh <instance> --discard-backup
#
# There is no --reindex in ElectrumX. A resync means an empty DB_DIRECTORY.
# This RENAMES rather than deletes, so rollback is one command and a few
# seconds; the cost is double the disk for the duration.
#
# The futures index is the only thing that needs this. Paged history,
# is_coinbase and the asset join all work against an existing database.
#
# Everything is derived from /etc/electrumx/<instance>.conf so that nothing
# has to be typed twice, and the recreated directory keeps the ORIGINAL
# owner, group and mode: on at least one deployment the service runs as
# `electrumx` while the database is owned by another user with group write,
# and a chown to the service account breaks it.

set -euo pipefail

CONF_DIR="${ELECTRUMX_CONF_DIR:-/etc/electrumx}"
SUFFIX=".pre-futures"

die() { echo "error: $*" >&2; exit 1; }

instance="${1:-}"
[ -n "$instance" ] || die "usage: $0 <instance> [--cache MB|--rollback|--discard-backup]"
shift || true

action="reindex"
cache=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rollback) action="rollback" ;;
    --discard-backup) action="discard" ;;
    --cache) cache="${2:?--cache needs a value in MB}"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

conf="$CONF_DIR/$instance.conf"
[ -r "$conf" ] || conf_readable=no
if [ "${conf_readable:-yes}" = no ]; then
  sudo test -r "$conf" || die "no readable conf at $conf"
  dbdir=$(sudo grep -oP '^DB_DIRECTORY\s*=\s*\K\S+' "$conf" || true)
else
  dbdir=$(grep -oP '^DB_DIRECTORY\s*=\s*\K\S+' "$conf" || true)
fi

[ -n "$dbdir" ] || die "no DB_DIRECTORY in $conf"
case "$dbdir" in
  /|/home|/home/*/|"$HOME") die "refusing to act on $dbdir" ;;
  /*) : ;;
  *) die "DB_DIRECTORY is not absolute: $dbdir" ;;
esac

backup="$dbdir$SUFFIX"
unit="electrumx@$instance"

case "$action" in
  rollback)
    [ -d "$backup" ] || die "no backup at $backup"
    echo "Rolling $unit back to $backup"
    sudo systemctl stop "$unit"
    sudo rm -rf "$dbdir"
    sudo mv "$backup" "$dbdir"
    sudo systemctl start "$unit"
    echo "done -- $unit restarted on the pre-reindex database"
    exit 0
    ;;
  discard)
    [ -d "$backup" ] || die "no backup at $backup"
    du -sh "$backup"
    read -r -p "Delete this backup permanently? type the instance name: " reply
    [ "$reply" = "$instance" ] || die "not confirmed"
    sudo rm -rf "$backup"
    echo "removed $backup"
    exit 0
    ;;
esac

[ -d "$dbdir" ] || die "$dbdir does not exist"
# `[ -e x ] && die` would exit under `set -e` when the test is FALSE, which is
# the normal case. An `if` says what is meant and cannot misfire.
if [ -e "$backup" ]; then
  die "$backup already exists -- roll back or discard it first"
fi

cache_line=""
if [ -n "$cache" ]; then
  cache_line="  CACHE_MB     -> $cache  (edits $conf)"
fi

owner=$(stat -c '%U:%G' "$dbdir")
mode=$(stat -c '%a' "$dbdir")
size=$(du -sh "$dbdir" | cut -f1)
avail=$(df -h "$dbdir" | awk 'NR==2 {print $4}')

cat <<PLAN

  instance     $instance   (unit $unit)
  database     $dbdir
  size         $size   (free on that filesystem: $avail)
  will move to $backup
  recreate as  $owner mode $mode
${cache_line}

  The service will be STOPPED and will then resync from genesis.
  That chain serves nothing until the sync completes.

PLAN

read -r -p "Proceed? type the instance name to confirm: " reply
[ "$reply" = "$instance" ] || die "not confirmed"

sudo systemctl stop "$unit"
sudo mv "$dbdir" "$backup"
sudo install -d -o "${owner%:*}" -g "${owner#*:}" -m "$mode" "$dbdir"
ls -ld "$dbdir" "$backup"

if [ -n "$cache" ]; then
  if sudo grep -q '^CACHE_MB' "$conf"; then
    sudo sed -i "s/^CACHE_MB.*/CACHE_MB=$cache/" "$conf"
  else
    echo "CACHE_MB=$cache" | sudo tee -a "$conf" >/dev/null
  fi
  sudo grep '^CACHE_MB' "$conf"
fi

date +'started %F %T'
sudo systemctl start "$unit"

cat <<NEXT

  Watch:     journalctl -fu $unit
  Rollback:  $0 $instance --rollback
  Reclaim:   $0 $instance --discard-backup

  Verify (the one that matters -- needs the server stopped briefly):
    sudo systemctl stop $unit
    python3 contrib/verify_futures_index.py $instance --scan 50000
    sudo systemctl start $unit

  Note the time above -- how long this takes decides whether mainnet is a
  full resync or worth writing a targeted backfill for.

NEXT
