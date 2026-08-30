#!/usr/bin/env python3
'''Ask a live ElectrumX what it thinks each of an address's outputs is.

    python3 contrib/check_spendability.py <instance> <address> [address...]

`instance` names a file in /etc/electrumx (rtm, rtm-testnet, ...); the TCP port
and the address version bytes come from it, so nothing has to be typed twice.

TWO CHECKS, NOT ONE. After a reindex you want the locked output of a futures
transaction to carry a `lock` AND every other output not to. An index that
marked everything locked would pass the first check on its own, and would take
the wallet's whole balance out of circulation.

No electrumx import: the scripthash is derived here from base58check, so this
runs under any python3 rather than only the interpreter the server is
installed into.
'''
import hashlib
import json
import re
import socket
import sys

CONF_DIR = '/etc/electrumx'
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
# P2PKH and P2SH version bytes, from the Raptoreum coin classes.
VERSIONS = {
    'mainnet': {0x3c: 'p2pkh', 0x10: 'p2sh'},
    'testnet': {0x7b: 'p2pkh', 0x13: 'p2sh'},
}


def conf(instance):
    with open(f'{CONF_DIR}/{instance}.conf') as handle:
        text = handle.read()
    values = dict(re.findall(r'^(\w+)\s*=\s*(\S+)', text, re.M))
    port = re.search(r'tcp://[^:]*:(\d+)', values.get('SERVICES', ''))
    if not port:
        sys.exit(f'no tcp:// port in SERVICES of {CONF_DIR}/{instance}.conf')

    return int(port.group(1)), values.get('NET', 'mainnet')


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


def scripthash(address, net):
    version, hash160 = b58check(address)
    kind = VERSIONS[net].get(version)
    if kind == 'p2pkh':
        script = b'\x76\xa9\x14' + hash160 + b'\x88\xac'
    elif kind == 'p2sh':
        script = b'\xa9\x14' + hash160 + b'\x87'
    else:
        sys.exit(f'{address}: version byte {version:#x} is not a {net} RTM address')

    return hashlib.sha256(script).digest()[::-1].hex()


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    instance, addresses = sys.argv[1], sys.argv[2:]
    port, net = conf(instance)

    seen = saw_is_coinbase = False

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
        print('server:', version.get('result'), f'(instance {instance}, {net}, tcp {port})')

        probe = call('blockchain.scripthash.get_history_page', ['00' * 32, 5])
        if 'error' in probe:
            print('get_history_page: NOT REGISTERED --', probe['error'].get('message'))
        else:
            print('get_history_page: registered')

        for address in addresses:
            answer = call('blockchain.scripthash.listunspent', [scripthash(address, net)])
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

        if seen and not saw_is_coinbase:
            print('\nWARNING: no entry carried is_coinbase -- the server is running '
                  'older code, or the restart did not pick up the new build')


if __name__ == '__main__':
    main()
