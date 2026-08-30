#!/usr/bin/env python3
'''Ask a live ElectrumX what it thinks each of an address's outputs is.

    python3 contrib/check_spendability.py <instance> <address> [address...]
    python3 contrib/check_spendability.py <instance> --coinbase [blocks-back]

`instance` names a file in /etc/electrumx (rtm, rtm-testnet, ...); the TCP port
and the address version bytes come from it, so nothing has to be typed twice.

TWO CHECKS, NOT ONE. After a reindex you want the locked output of a futures
transaction to carry a `lock` AND every other output not to. An index that
marked everything locked would pass the first check on its own, and would take
the wallet's whole balance out of circulation.

`--coinbase` finds its own subject: it walks back from the tip asking the
daemon for block coinbases until it finds one whose output is still unspent,
then checks that. A recent coinbase is unspendable for 100 confirmations, so
there is nearly always one to find -- and it is the only way to exercise
`is_coinbase` without a funded address. That derivation reads
tx_counts[height - 1] and is the one place a wrong answer means offering coins
consensus will not let anyone move.

No electrumx import: the scripthash is derived here from base58check, so this
runs under any python3 rather than only the interpreter the server is
installed into. `--coinbase` additionally talks to the coin daemon, using
DAEMON_URL from the same conf.
'''
import base64
import hashlib
import json
import re
import socket
import sys
import urllib.parse
import urllib.request

CONF_DIR = '/etc/electrumx'
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
# P2PKH and P2SH version bytes, transcribed from the coin classes in
# electrumx/lib/coins.py. Keyed by (COIN, NET), which is what the instance conf
# states -- the first version of this assumed Raptoreum and refused every
# Osmium address as "not a mainnet RTM address", which is a script bug wearing
# the costume of a finding.
VERSIONS = {
    ('Raptoreum', 'mainnet'): {0x3c: 'p2pkh', 0x10: 'p2sh'},
    ('Raptoreum', 'testnet'): {0x7b: 'p2pkh', 0x13: 'p2sh'},
    ('Osmium', 'mainnet'): {0x3f: 'p2pkh', 0x0f: 'p2sh'},
}


def conf(instance):
    with open(f'{CONF_DIR}/{instance}.conf') as handle:
        text = handle.read()
    values = dict(re.findall(r'^(\w+)\s*=\s*(\S+)', text, re.M))
    port = re.search(r'tcp://[^:]*:(\d+)', values.get('SERVICES', ''))
    if not port:
        sys.exit(f'no tcp:// port in SERVICES of {CONF_DIR}/{instance}.conf')

    coin, net = values.get('COIN', 'Raptoreum'), values.get('NET', 'mainnet')
    if (coin, net) not in VERSIONS:
        sys.exit(f'no address version bytes recorded for {coin}/{net} -- add them to VERSIONS '
                 f'from that coin class in electrumx/lib/coins.py')

    return int(port.group(1)), (coin, net), values.get('DAEMON_URL')


def daemon_rpc(url):
    parts = urllib.parse.urlsplit(url)
    endpoint = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path or '/', '', ''))
    creds = base64.b64encode(
        f"{parts.username or ''}:{parts.password or ''}".encode()).decode()
    headers = {'Content-Type': 'application/json', 'Authorization': 'Basic ' + creds}

    def rpc(method, params):
        body = json.dumps({'jsonrpc': '1.0', 'id': 'c', 'method': method, 'params': params})
        req = urllib.request.Request(endpoint, body.encode(), headers)

        return json.load(urllib.request.urlopen(req, timeout=60))['result']

    return rpc


def recent_coinbase_addresses(rpc, back):
    '''Addresses paid by a recent coinbase whose output is still unspent.

    Walks back from the tip rather than taking the tip itself: the newest
    blocks are the most likely to be unspent, but a miner that sweeps
    immediately would otherwise make this look broken.
    '''
    tip = rpc('getblockcount', [])
    found = []
    for height in range(tip, max(tip - back, 1), -1):
        block = rpc('getblock', [rpc('getblockhash', [height]), 2])
        coinbase = block['tx'][0]
        for vout in coinbase['vout']:
            spk = vout['scriptPubKey']
            addr = spk.get('address') or (spk.get('addresses') or [None])[0]
            if not addr:
                continue
            if rpc('gettxout', [coinbase['txid'], vout['n'], False]):
                print(f'coinbase of block {height:,d} -> {addr} (vout {vout["n"]}, unspent)')
                found.append(addr)
                if len(found) >= 2:
                    return found
    return found


def b58check(address):
    number = 0
    for char in address:
        if char not in B58:
            sys.exit(f'{address}: not base58')
        number = number * 58 + B58.index(char)
    raw = number.to_bytes(25, 'big')
    if hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        sys.exit(f'{address}: bad checksum')

    return raw[0], raw[1:21]


def scripthash(address, coin_net):
    version, hash160 = b58check(address)
    kind = VERSIONS[coin_net].get(version)
    if kind == 'p2pkh':
        script = b'\x76\xa9\x14' + hash160 + b'\x88\xac'
    elif kind == 'p2sh':
        script = b'\xa9\x14' + hash160 + b'\x87'
    else:
        sys.exit(f'{address}: version byte {version:#x} is not a '
                 f'{coin_net[0]} {coin_net[1]} address')

    return hashlib.sha256(script).digest()[::-1].hex()


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    instance, addresses = sys.argv[1], sys.argv[2:]
    port, coin_net, url = conf(instance)

    if addresses and addresses[0] == '--coinbase':
        if not url:
            sys.exit('no DAEMON_URL in the conf, so --coinbase cannot find a subject')
        back = int(addresses[1]) if len(addresses) > 1 else 200
        addresses = recent_coinbase_addresses(daemon_rpc(url), back)
        if not addresses:
            sys.exit(f'no unspent coinbase output in the last {back} blocks')

    seen = saw_is_coinbase = any_coinbase = False

    with socket.create_connection(('127.0.0.1', port), timeout=60) as sock:
        def call(method, params):
            payload = json.dumps({'id': 0, 'jsonrpc': '2.0', 'method': method, 'params': params})
            sock.sendall((payload + '\n').encode())
            buf = b''
            while not buf.endswith(b'\n'):
                chunk = sock.recv(65536)
                if not chunk:
                    sys.exit('server closed the connection')
                buf += chunk

            return json.loads(buf)

        version = call('server.version', ['check_spendability', '1.4'])
        print('server:', version.get('result'),
              f'(instance {instance}, {coin_net[0]} {coin_net[1]}, tcp {port})')

        probe = call('blockchain.scripthash.get_history_page', ['00' * 32, 5])
        if 'error' in probe:
            print('get_history_page: NOT REGISTERED --', probe['error'].get('message'))
        else:
            print('get_history_page: registered')

        for address in addresses:
            answer = call('blockchain.scripthash.listunspent',
                          [scripthash(address, coin_net)])
            if 'error' in answer:
                print(f'\n{address}: {answer["error"]}')
                continue
            utxos = answer['result']
            print(f'\n{address}  ({len(utxos)} unspent)')
            for utxo in utxos:
                seen = True
                saw_is_coinbase = saw_is_coinbase or 'is_coinbase' in utxo
                flags = []
                if utxo.get('is_coinbase'):
                    any_coinbase = True
                    flags.append('COINBASE')
                if 'asset' in utxo:
                    asset = utxo['asset']
                    flags.append(f'ASSET {asset.get("name") or asset["asset_id"][:12]}'
                                 f' x{asset["amount"]}')
                if 'lock' in utxo:
                    lock = utxo['lock']
                    flags.append('LOCK ' + ('unreadable payload' if not lock['readable']
                                            else f'maturity={lock["maturity"]}'
                                                 f' locktime={lock["locktime"]}'))
                print(f'  {utxo["tx_hash"][:16]}…:{utxo["tx_pos"]}'
                      f'  h={utxo["height"]}  {utxo["value"]}'
                      f'  {" | ".join(flags) or "plain"}')

        if seen and not any_coinbase and '--coinbase' in sys.argv:
            print('\nWARNING: an unspent coinbase output was NOT flagged is_coinbase=true.\n'
                  'That derivation is wrong, and immature coinbases will be offered as\n'
                  'ordinary spendable money.')
        if seen and not saw_is_coinbase:
            print('\nWARNING: no entry carried is_coinbase -- the server is running '
                  'older code, or the restart did not pick up the new build')


if __name__ == '__main__':
    main()
