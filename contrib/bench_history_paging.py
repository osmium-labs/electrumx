#!/usr/bin/env python3
'''Measure blockchain.scripthash.get_history against get_history_page.

WHY THIS EXISTS AS A SCRIPT RATHER THAN A CLAIM.  The paged read is obviously
cheaper for a large history and obviously not free for a small one: it trades
the session manager's whole-history RAM cache for a few dozen fresh reads from
the hashes file.  Which side of that an address falls on depends on how many
transactions it actually has, and that is a property of the deployment, not of
the code.  Run it before believing either number.

    python3 contrib/bench_history_paging.py <host> <port> <address> [address...]

Addresses are converted to scripthashes locally, so this needs electrumx on the
path but no server-side access beyond the ElectrumX TCP port.  It is read-only.

It reports, per address, for each method: the wall-clock of a cold call, the
median of REPEATS warm calls, the number of entries returned, and the size of
the JSON response.  The last is usually the number that decides it -- the paged
call returns a fixed page where get_history returns the entire history, so the
gap grows with the address rather than staying put.
'''
import asyncio
import json
import statistics
import sys
import time

from aiorpcx import connect_rs, timeout_after

from electrumx.lib.coins import Coin

REPEATS = 5
PAGE = 50


def scripthash(coin, address):
    from electrumx.lib.hash import sha256, hash_to_hex_str

    return hash_to_hex_str(sha256(coin.pay_to_address_script(address)))


async def timed(session, method, params):
    start = time.perf_counter()
    async with timeout_after(60):
        result = await session.send_request(method, params)

    return time.perf_counter() - start, result


async def measure(session, sh):
    full = [await timed(session, 'blockchain.scripthash.get_history', [sh])
            for _ in range(REPEATS + 1)]
    paged = [await timed(session, 'blockchain.scripthash.get_history_page', [sh, PAGE])
             for _ in range(REPEATS + 1)]

    return {
        'full_cold': full[0][0],
        'full_warm': statistics.median(t for t, _ in full[1:]),
        'full_entries': len(full[0][1]),
        'full_bytes': len(json.dumps(full[0][1])),
        'page_cold': paged[0][0],
        'page_warm': statistics.median(t for t, _ in paged[1:]),
        'page_entries': len(paged[0][1]['history']),
        'page_bytes': len(json.dumps(paged[0][1])),
    }


async def main(host, port, addresses):
    coin = Coin.lookup_coin_class('Raptoreum', 'mainnet')
    async with connect_rs(host, int(port)) as session:
        session.transport._framer.max_size = 0
        for address in addresses:
            try:
                row = await measure(session, scripthash(coin, address))
            except Exception as exc:                       # noqa: BLE001
                print(f'{address}: {exc}')
                continue
            print(f'{address}')
            print(f'  get_history       {row["full_entries"]:>7,d} entries  '
                  f'{row["full_bytes"]:>9,d} B  '
                  f'cold {row["full_cold"]*1000:7.1f} ms  warm {row["full_warm"]*1000:7.1f} ms')
            print(f'  get_history_page  {row["page_entries"]:>7,d} entries  '
                  f'{row["page_bytes"]:>9,d} B  '
                  f'cold {row["page_cold"]*1000:7.1f} ms  warm {row["page_warm"]*1000:7.1f} ms')
            print(f'  bytes saved: {row["full_bytes"] - row["page_bytes"]:,d} '
                  f'({100 * (1 - row["page_bytes"] / max(row["full_bytes"], 1)):.1f}%)')


if __name__ == '__main__':
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
