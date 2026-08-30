#!/usr/bin/env python3
'''Check the futures index against the daemon, in both directions.

    contrib/verify_futures_index.py <instance> [--scan N] [--scan-from HEIGHT]
    contrib/verify_futures_index.py <instance> --dump FILE          # server stopped, seconds
    contrib/verify_futures_index.py <instance> --check FILE [--from H] [--to H]

LevelDB admits one process, so the server has to be stopped. THIS SCRIPT DOES
THAT ITSELF and restarts it afterwards, including on failure, because the
hand-written version of that sequence put a minutes-long command between the
stop and the start -- and the start got separated from it and never ran. The
instance stayed down for nearly four hours, and the first thing to notice was a
wallet failing to poll.

    python3 contrib/verify_futures_index.py <instance> --scan 50000

Pass --no-restart to manage the service yourself. It still warns on exit if it
left the instance stopped.

WHY NOT CHECK listunspent INSTEAD. Because there is probably nothing to see.
Raptoreum futures are rare and the ones that exist have long since released
and been spent -- the known real one, mainnet a4e09d74… at height 426,615,
carries lockTime 300, so it unlocked five minutes after confirming in October
2022. An index verified only through listunspent would be verified against an
empty result, which every broken index also produces.

TWO DIRECTIONS, and the second is the one that matters:

  forward   every record we hold decodes to what the daemon says about that
            transaction. Catches a wrong offset or a sign error.

  reverse   every type-7 transaction in the scanned range HAS a record.
            Catches the fail-open failure -- an index that is a subset of the
            truth reads as "nothing is locked", and locked outputs get offered
            as ordinary money.

SCAN WHERE THE FUTURES ARE. The reverse pass defaults to counting back from
the tip, and on testnet that window held no type-7 transactions at all: it
reported "0 with no record" while checking nothing. A range with nothing in it
passes whatever the index does. This prints the height distribution of the
records it holds so the scan can be aimed with --scan-from, and refuses to
call an empty reverse pass a result.

A FULL-RANGE REVERSE SCAN WITHOUT THE OUTAGE. The reverse pass needs two
things: the txids the index holds, and a walk of the chain. Only the first
touches LevelDB. `--dump` writes them to a file in seconds with the server
stopped; `--check` then walks the daemon against that file with the server
running normally. That turns a million-block scan from a long outage into a
few seconds of downtime plus a background job.

    sudo systemctl stop electrumx@rtm
    ... --dump /tmp/rtm-futures.json
    sudo systemctl start electrumx@rtm
    ... --check /tmp/rtm-futures.json --from 420420

Read-only against both the database and the daemon.
'''
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

CONF_DIR = os.environ.get('ELECTRUMX_CONF_DIR', '/etc/electrumx')


def service_active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


class ServiceStopped:
    '''Stop the unit for the body, and put it back however the body exits.

    A context manager rather than two calls, because the failure this exists for
    is the restart not running -- and the one shape that cannot happen is a
    `finally` that is never reached.
    '''

    def __init__(self, unit, manage=True):
        self.unit = unit
        self.manage = manage
        self.was_active = False

    def __enter__(self):
        self.was_active = service_active(self.unit)
        if self.manage and self.was_active:
            print(f'stopping {self.unit}', flush=True)
            subprocess.run(['sudo', 'systemctl', 'stop', self.unit], check=True)

        return self

    def __exit__(self, *_exc):
        if self.manage and self.was_active:
            print(f'restarting {self.unit}', flush=True)
            subprocess.run(['sudo', 'systemctl', 'start', self.unit], check=False)
        elif not service_active(self.unit):
            print(f'\n*** {self.unit} IS NOT RUNNING and this script did not start it. ***\n'
                  f'*** Start it:  sudo systemctl start {self.unit}                  ***')

        return False


def arg_value(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def load_conf(instance):
    path = f'{CONF_DIR}/{instance}.conf'
    try:
        with open(path) as handle:
            text = handle.read()
    except PermissionError:
        sys.exit(f'cannot read {path} -- re-run with sudo, or as a user in its group')
    values = dict(re.findall(r'^(\w+)\s*=\s*(\S+)', text, re.M))
    os.environ.update(values)

    return values


def rpc_caller(url):
    parts = urllib.parse.urlsplit(url)
    endpoint = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path or '/', '', ''))
    creds = base64.b64encode(
        f"{parts.username or ''}:{parts.password or ''}".encode()).decode()
    headers = {'Content-Type': 'application/json', 'Authorization': 'Basic ' + creds}

    def rpc(method, params):
        body = json.dumps({'jsonrpc': '1.0', 'id': 'v', 'method': method, 'params': params})
        req = urllib.request.Request(endpoint, body.encode(), headers)

        return json.load(urllib.request.urlopen(req, timeout=60))['result']

    return rpc


def daemon_lock(tx):
    '''(maturity, lock_time, lock_output_index) as the DAEMON reports them.'''
    future = tx.get('futureTx') or {}
    if 'lockOutputIndex' not in future:
        return None
    maturity, lock_time = future.get('maturity'), future.get('lockTime')

    return (None if maturity in (None, -1) else maturity,
            None if lock_time in (None, -1) else lock_time,
            future['lockOutputIndex'])


async def _run():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    instance = sys.argv[1]
    scan = 0
    scan_from = None
    if '--scan' in sys.argv:
        scan = int(sys.argv[sys.argv.index('--scan') + 1])
    if '--scan-from' in sys.argv:
        scan_from = int(sys.argv[sys.argv.index('--scan-from') + 1])
        scan = scan or 50000

    dump_to = arg_value('--dump')
    check_file = arg_value('--check')

    values = load_conf(instance)

    if check_file:
        # No database access at all, so the server can stay up.
        held = json.load(open(check_file))
        rpc = rpc_caller(values['DAEMON_URL'])
        low = int(arg_value('--from') or 1)
        high = int(arg_value('--to') or rpc('getblockcount', []))
        print(f'checking {high:,d} down to {low:,d} against {len(held):,d} '
              f'records dumped from {check_file}', flush=True)
        seen = missing = 0
        for height in range(high, low - 1, -1):
            for tx in rpc('getblock', [rpc('getblockhash', [height]), 2])['tx']:
                if tx.get('type') != 7:
                    continue
                seen += 1
                if tx['txid'] not in held:
                    missing += 1
                    print(f'  MISSING {tx["txid"]} at height {height:,d}')
            if height % 20000 == 0:
                print(f'  ... {height:,d}  ({seen:,d} found so far)', flush=True)
        print(f'\n{seen:,d} type-7 transactions in range, {missing:,d} with no record')
        print('RESULT:', 'OK' if not missing else f'{missing} MISSING -- do not trust `lock`')

        return 1 if missing else 0

    try:
        from electrumx.server.db import DB, FUTURES_PREFIX, FUTURES_UNREADABLE
        from electrumx.server.env import Env
        from electrumx.server.history import TXNUM_LEN
        from electrumx.lib.hash import hash_to_hex_str
        from electrumx.lib.util import unpack_le_int32_from, unpack_le_uint16_from, unpack_le_uint64
    except ImportError as exc:
        sys.exit(f'{exc}\nRun this with the interpreter electrumx is installed into '
                 f'(see: head -1 $(command -v electrumx_server))')

    rpc = rpc_caller(values['DAEMON_URL'])
    try:
        # DB() resolves the storage backend, so a missing plyvel fails HERE
        # rather than at open_for_serving. Both are the same problem to whoever
        # is running this, so both are one message.
        db = DB(Env())
        await db.open_for_serving()
    except ModuleNotFoundError as exc:
        sys.exit(f'{exc} -- run this with the interpreter electrumx is installed into '
                 f'(see: head -1 $(command -v electrumx_server))')
    except Exception as exc:                                      # noqa: BLE001
        sys.exit(f'could not open the database ({exc}).\nIs electrumx@{instance} still running? '
                 f'LevelDB admits one process.')

    print(f'{instance}: db height {db.db_height:,d}, {db.db_tx_count:,d} transactions')

    # ---- forward -------------------------------------------------------
    padding = bytes(8 - TXNUM_LEN)
    records = {}
    for key, value in db.utxo_db.iterator(prefix=FUTURES_PREFIX):
        tx_num, = unpack_le_uint64(key[-TXNUM_LEN:] + padding)
        maturity, = unpack_le_int32_from(value, 0)
        lock_time, = unpack_le_int32_from(value, 4)
        lock_idx, = unpack_le_uint16_from(value, 8)
        records[tx_num] = (None if maturity < 0 else maturity,
                           None if lock_time < 0 else lock_time,
                           lock_idx)
    print(f'futures records held: {len(records):,d}')
    if not records:
        print('\nNONE. Either this database predates the futures index (reindex not done)\n'
              'or the chain genuinely has no type-7 transactions.')

    checked = mismatched = unreadable = 0
    by_txid = {}
    heights = []
    for tx_num, mine in sorted(records.items()):
        tx_hash, height = db.fs_tx_hash(tx_num)
        if tx_hash is None:
            print(f'  tx_num {tx_num}: no hash on disk')
            continue
        txid = hash_to_hex_str(tx_hash)
        by_txid[txid] = mine
        heights.append(height)
        if mine[2] == FUTURES_UNREADABLE:
            unreadable += 1
            print(f'  {txid} h={height:,d}  recorded UNREADABLE (all outputs withheld)')
            continue
        theirs = daemon_lock(rpc('getrawtransaction', [txid, 1]))
        checked += 1
        if theirs != mine:
            mismatched += 1
            print(f'  MISMATCH {txid} h={height:,d}\n    index  {mine}\n    daemon {theirs}')
    print(f'forward: {checked:,d} checked against the daemon, {mismatched:,d} mismatched, '
          f'{unreadable:,d} recorded unreadable')

    if dump_to:
        with open(dump_to, 'w') as handle:
            json.dump({txid: list(lock) for txid, lock in by_txid.items()}, handle)
        print(f'wrote {len(by_txid):,d} txids to {dump_to} -- '
              f'restart the server, then re-run with --check {dump_to}')
        db.utxo_db.close()
        db.history.close_db()

        return 0

    if heights:
        heights.sort()
        median = heights[len(heights) // 2]
        print(f'records span heights {heights[0]:,d} .. {heights[-1]:,d} '
              f'(median {median:,d})')
        if scan and scan_from is None:
            # Centre the window on the MEDIAN, not the newest record. Aiming at
            # the newest one hit the sparse tail: 100,000 blocks back from the
            # highest record found exactly one type-7, because half the records
            # sit below the median by definition and the tail is thin.
            scan_from = min(median + scan // 2, heights[-1])
            print(f'aiming the reverse scan at {scan_from:,d}, centred on the median, '
                  f'because that is where the density is')

    # ---- reverse -------------------------------------------------------
    seen = missing = in_window = 0
    if scan:
        start = scan_from if scan_from is not None else rpc('getblockcount', [])
        low = max(start - scan, 1)
        in_window = sum(1 for h in heights if low <= h <= start)
        print(f'\nreverse: scanning {start:,d} down to {low:,d}. '
              f'{in_window:,d} of the {len(heights):,d} records held fall in that range.',
              flush=True)
        for height in range(start, max(start - scan, 1), -1):
            for tx in rpc('getblock', [rpc('getblockhash', [height]), 2])['tx']:
                if tx.get('type') != 7:
                    continue
                seen += 1
                if tx['txid'] not in by_txid:
                    missing += 1
                    print(f'  MISSING {tx["txid"]} at height {height:,d}')
            if height % 5000 == 0:
                print(f'  ... {height:,d}', flush=True)
        print(f'reverse: {seen:,d} type-7 transactions in range, {missing:,d} with no record')
        if seen and not missing:
            print('  every one of them is indexed')
        # An EQUALITY, not a subset. Records in the window with no type-7 behind
        # them is the opposite bug to a missing record -- harmless to spend but
        # it would withhold coins that were never locked, and nothing else here
        # would notice it.
        if seen and in_window != seen:
            print(f'  MISMATCH: {in_window:,d} records in this range but {seen:,d} '
                  f'type-7 transactions -- the index and the chain disagree on the count')
        elif not seen:
            # An empty range passes whatever the index does, so it is not a
            # pass. Saying so is the difference between a check and a comfort.
            print('  NOTHING TO CHECK -- no type-7 transaction in that range, so this\n'
                  '  pass proved nothing. Re-run aimed at the records, e.g.\n'
                  f'  --scan-from {heights[-1] if heights else "<height>"} --scan 100000')

    db.utxo_db.close()
    db.history.close_db()

    count_off = scan and seen and in_window != seen
    bad = mismatched + (missing if scan else 0) + (1 if count_off else 0)
    vacuous = scan and not seen
    if bad:
        print(f'\nRESULT: {bad} PROBLEM(S) -- do not trust `lock` yet')
    elif vacuous:
        print('\nRESULT: forward OK, reverse INCONCLUSIVE (empty range)')
    else:
        print('\nRESULT: OK')

    return 1 if bad else 0


async def main():
    """Manage the service around the database work, and only around that.

    `--check` touches no database, so the server stays up for it -- that mode
    exists precisely so a million-block scan does not need an outage.
    """
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if '--check' in sys.argv:
        return await _run()

    unit = f'electrumx@{sys.argv[1]}'
    with ServiceStopped(unit, manage='--no-restart' not in sys.argv):
        return await _run()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
