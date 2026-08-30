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


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rpc = rpc_caller(daemon_url(sys.argv[1]))
    back = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

    tip = rpc('getblockcount', [])
    print(f'tip {tip:,d} -- scanning back up to {back:,d} blocks', flush=True)
    found = 0
    for height in range(tip, max(tip - back, 1), -1):
        for tx in rpc('getblock', [rpc('getblockhash', [height]), 2])['tx']:
            if tx.get('type') != 7:
                continue
            found += 1
            print(f'\nheight {height:,d}  txid {tx["txid"]}')
            print('  futureTx:    ', json.dumps(tx.get('futureTx')))
            print('  extraPayload:', tx.get('extraPayload'))
            for vout in tx['vout']:
                spk = vout['scriptPubKey']
                addr = spk.get('address') or (spk.get('addresses') or [None])[0]
                print(f'  vout {vout["n"]}  {vout["value"]}  {addr}')
            if found >= 3:
                print('\nThe vout named by futureTx.lockOutputIndex is the only one that '
                      'should carry a "lock" after the reindex.')

                return
        if height % 2000 == 0:
            print(f'... {height:,d}', flush=True)
    print(f'\nfound {found} in that range')


if __name__ == '__main__':
    main()
