#!/usr/bin/env python3
'''Find type-7 (futures) transactions by asking the daemon.

    python3 contrib/rtm_scan_futures.py <instance> [blocks-back]

`instance` names a file in /etc/electrumx (rtm, rtm-testnet, ...); the daemon
URL is read from it, so no credential is ever typed or echoed.

Prints the daemon's own `futureTx` decode alongside the raw `extraPayload`,
which is what `Raptoreum.parse_futures_payload` reads. The two disagreeing is
the thing worth catching before a reindex, not after: the asset payload's
layout turned out to be about unique-vs-non-unique rather than divisibility,
and it was a real payload that said so.

IT ALSO REPORTS WHETHER THE LOCKED OUTPUT IS STILL UNSPENT, which is what
makes a result usable as a verification target. The known real one -- mainnet
a4e09d74… at height 426,615 -- has lockTime 300, so it released five minutes
after confirming in October 2022 and will not appear in any listunspent. A
futures transaction that is interesting to a parser and one that is useful for
checking an index are not the same transaction.

Read-only. Talks to the coin daemon, not to ElectrumX.
'''
import base64
import json
import re
import sys
import urllib.parse
import urllib.request

CONF_DIR = '/etc/electrumx'


def daemon_url(instance):
    with open(f'{CONF_DIR}/{instance}.conf') as conf:
        match = re.search(r'^DAEMON_URL\s*=\s*(\S+)', conf.read(), re.M)
    if not match:
        sys.exit(f'no DAEMON_URL in {CONF_DIR}/{instance}.conf')

    return match.group(1)


def rpc_caller(url):
    parts = urllib.parse.urlsplit(url)
    # urllib does not take credentials from a URL -- it hands the whole
    # `user:pass@host` to DNS and fails to resolve it. curl does, which is why
    # the same URL works there. Split them out and send Basic ourselves.
    endpoint = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path or '/', '', ''))
    creds = base64.b64encode(
        f"{parts.username or ''}:{parts.password or ''}".encode()).decode()
    headers = {'Content-Type': 'application/json', 'Authorization': 'Basic ' + creds}

    def rpc(method, params):
        body = json.dumps({'jsonrpc': '1.0', 'id': 'x', 'method': method, 'params': params})
        req = urllib.request.Request(endpoint, body.encode(), headers)

        return json.load(urllib.request.urlopen(req, timeout=60))['result']

    return rpc


def locked_index(tx):
    '''Which output the payload locks, from the daemon or from the bytes.

    Offsets verified against the daemon's own decode of mainnet
    a4e09d74…bd3fb0e3: maturity int32 at 2, lockTime int32 at 6,
    lockOutputIndex uint16 at 10. Falls back to the raw payload because a
    daemon that does not decode futureTx would otherwise make this script
    silently find nothing.
    '''
    decoded = tx.get('futureTx') or {}
    if 'lockOutputIndex' in decoded:
        return decoded['lockOutputIndex']
    raw = tx.get('extraPayload')
    if not raw or len(raw) < 24:
        return None

    return int.from_bytes(bytes.fromhex(raw)[10:12], 'little')


def report(rpc, height, tx):
    idx = locked_index(tx)
    unspent = rpc('gettxout', [tx['txid'], idx, True]) if idx is not None else None
    mark = ('UNSPENT -- usable as a verification target' if unspent
            else 'spent (or released and moved)')
    print(f'\nheight {height:,d}  txid {tx["txid"]}')
    print('  futureTx:    ', json.dumps(tx.get('futureTx')))
    print('  extraPayload:', tx.get('extraPayload'))
    for vout in tx['vout']:
        spk = vout['scriptPubKey']
        addr = spk.get('address') or (spk.get('addresses') or [None])[0]
        flag = '  <-- LOCKED, ' + mark if vout['n'] == idx else ''
        print(f'  vout {vout["n"]}  {vout["value"]}  {addr}{flag}')

    if not unspent or idx is None:
        return False, None
    spk = tx['vout'][idx]['scriptPubKey']

    return True, spk.get('address') or (spk.get('addresses') or [None])[0]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    instance = sys.argv[1]
    rpc = rpc_caller(daemon_url(instance))
    back = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

    tip = rpc('getblockcount', [])
    print(f'tip {tip:,d} -- scanning back up to {back:,d} blocks', flush=True)
    found = 0
    targets = []
    for height in range(tip, max(tip - back, 1), -1):
        for tx in rpc('getblock', [rpc('getblockhash', [height]), 2])['tx']:
            if tx.get('type') != 7:
                continue
            found += 1
            is_unspent, address = report(rpc, height, tx)
            if is_unspent and address:
                targets.append(address)
            # Stop on the first UNSPENT one -- that is the one worth having --
            # or after a few spent ones, which are enough to check the payload.
            if targets or found >= 5:
                break
        else:
            if height % 2000 == 0:
                print(f'... {height:,d}', flush=True)
            continue
        break

    print(f'\nfound {found} futures transaction(s)')
    if targets:
        print('\nVerify after the reindex with:')
        print(f'  python3 contrib/check_spendability.py {instance} {targets[0]}')
        print('The locked vout must carry a "lock"; every other output must not.')
    elif found:
        print('\nNone still unspent in that range, so none is usable for the listunspent\n'
              'check. Either widen the scan, or send a fresh futures transaction\n'
              'and verify against that.')


if __name__ == '__main__':
    main()
