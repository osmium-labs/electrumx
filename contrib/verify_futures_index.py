#!/usr/bin/env python3
'''Check the futures index against the daemon, in both directions.

    contrib/verify_futures_index.py <instance> [--scan N]

STOP THE SERVER FIRST -- LevelDB admits one process:

    sudo systemctl stop electrumx@<instance>
    python3 contrib/verify_futures_index.py <instance> --scan 50000
    sudo systemctl start electrumx@<instance>

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

Read-only against both the database and the daemon.
'''
import asyncio
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request

CONF_DIR = os.environ.get('ELECTRUMX_CONF_DIR', '/etc/electrumx')


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


async def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    instance = sys.argv[1]
    scan = 0
    if '--scan' in sys.argv:
        scan = int(sys.argv[sys.argv.index('--scan') + 1])

    values = load_conf(instance)
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
    for tx_num, mine in sorted(records.items()):
        tx_hash, height = db.fs_tx_hash(tx_num)
        if tx_hash is None:
            print(f'  tx_num {tx_num}: no hash on disk')
            continue
        txid = hash_to_hex_str(tx_hash)
        by_txid[txid] = mine
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

    # ---- reverse -------------------------------------------------------
    if scan:
        tip = rpc('getblockcount', [])
        missing = seen = 0
        print(f'\nreverse: scanning {scan:,d} blocks back from {tip:,d} '
              f'for type-7 transactions with no record', flush=True)
        for height in range(tip, max(tip - scan, 1), -1):
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

    db.utxo_db.close()
    db.history.close_db()

    bad = mismatched + (missing if scan else 0)
    print('\nRESULT:', 'OK' if not bad else f'{bad} PROBLEM(S) -- do not trust `lock` yet')

    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
