"""Tests for RaptoreumElectrumX vin value enrichment.

The Raptoreum daemon does not include value/valueSat/address fields
in vin entries of getrawtransaction verbose output.
RaptoreumElectrumX.transaction_get enriches each non-coinbase vin by
batch-fetching parent transactions and reading the referenced outputs.
"""

import asyncio
from unittest import mock

import pytest

from electrumx.lib.coins import Raptoreum, RaptoreumTestnet
from electrumx.lib.hash import hash_to_hex_str
from electrumx.lib.tx import TxOutput, TxInput
from electrumx.lib.tx_dash import DashTx
from electrumx.server.session import RaptoreumElectrumX


def _build_raw_tx(outputs, tx_type=0, version=2):
    """Build a minimal serialized DashTx with the given outputs."""
    coinbase_input = TxInput(
        prev_hash=bytes(32), prev_idx=0xffffffff,
        script=b'\x04' + b'\xff' * 4, sequence=0xffffffff,
    )
    tx = DashTx(
        version=version,
        inputs=[coinbase_input],
        outputs=outputs,
        locktime=0,
        tx_type=tx_type,
        extra_payload=b'',
        txid=b'\x00' * 32,
        wtxid=b'\x00' * 32,
    )
    return tx.serialize()


def _p2pkh_script(hash160: bytes) -> bytes:
    """Return a standard P2PKH script for 20-byte hash160."""
    return (b'\x76\xa9\x14' + hash160 + b'\x88\xac')


def _p2sh_script(hash160: bytes) -> bytes:
    """Return a standard P2SH script for 20-byte hash160."""
    return (b'\xa9\x14' + hash160 + b'\x87')


def _make_session(coin_cls=Raptoreum):
    """Create a minimal mock RaptoreumElectrumX instance."""
    session = mock.AsyncMock(spec=RaptoreumElectrumX)
    session.coin = coin_cls
    session.daemon_request = mock.AsyncMock()

    # Bind the real methods to the mock so we can test them
    session._fix_vin_values = (
        RaptoreumElectrumX._fix_vin_values.__get__(session))
    session._address_from_pk_script = (
        RaptoreumElectrumX._address_from_pk_script.__get__(session))
    session.transaction_get = (
        RaptoreumElectrumX.transaction_get.__get__(session))
    return session


# -- _address_from_pk_script tests ------------------------------------------

def test_p2pkh_address():
    session = _make_session()
    hash160 = bytes(range(20))
    script = _p2pkh_script(hash160)
    addr = session._address_from_pk_script(script)
    # Should decode back to the same hash160
    raw = Raptoreum.DECODE_CHECK(addr)
    assert raw == Raptoreum.P2PKH_VERBYTE + hash160


def test_p2sh_address():
    session = _make_session()
    hash160 = bytes(range(20))
    script = _p2sh_script(hash160)
    addr = session._address_from_pk_script(script)
    raw = Raptoreum.DECODE_CHECK(addr)
    assert raw == Raptoreum.P2SH_VERBYTES[0] + hash160


def test_p2pkh_with_asset_payload():
    session = _make_session()
    hash160 = bytes(range(20))
    base_script = _p2pkh_script(hash160)
    # Append asset payload: OP_ASSET_ID(bc) + junk
    asset_payload = bytes([0xbc, 0x4c, 0x55]) + b'\x00' * 85 + bytes([0x75])
    script = base_script + asset_payload
    addr = session._address_from_pk_script(script)
    raw = Raptoreum.DECODE_CHECK(addr)
    assert raw == Raptoreum.P2PKH_VERBYTE + hash160


def test_unknown_script_returns_none():
    session = _make_session()
    addr = session._address_from_pk_script(b'\x00\x01\x02')
    assert addr is None


# -- _fix_vin_values tests ---------------------------------------------------

@pytest.mark.asyncio
async def test_fix_vin_no_suspects():
    """When all vin entries have nonzero valueSat, result is untouched."""
    session = _make_session()
    result = {
        'vin': [
            {'txid': 'aa' * 32, 'vout': 0, 'valueSat': 100, 'value': 0.000001},
        ],
        'vout': [],
    }
    patched = await session._fix_vin_values(result)
    assert patched['vin'][0]['valueSat'] == 100
    session.daemon_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_fix_vin_coinbase_ignored():
    """Coinbase inputs (valueSat=0) must not trigger parent lookups."""
    session = _make_session()
    result = {
        'vin': [
            {'coinbase': 'deadbeef', 'sequence': 0xffffffff},
        ],
        'vout': [],
    }
    patched = await session._fix_vin_values(result)
    assert patched is result
    session.daemon_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_fix_vin_patches_value():
    """Vin entries missing valueSat get enriched from the parent tx."""
    session = _make_session()

    hash160 = bytes(range(20))
    pk_script = _p2pkh_script(hash160)
    parent_value = 375_000_000_000  # 3750 RTM

    parent_raw = _build_raw_tx([
        TxOutput(value=parent_value, pk_script=pk_script),
    ])
    parent_txid = 'bb' * 32

    result = {
        'vin': [
            {
                'txid': parent_txid, 'vout': 0,
                'scriptSig': {'asm': '', 'hex': ''},
                'sequence': 0xffffffff,
            },
        ],
        'vout': [],
    }

    session.daemon_request = mock.AsyncMock(return_value=[parent_raw])

    patched = await session._fix_vin_values(result)

    assert patched['vin'][0]['valueSat'] == parent_value
    assert patched['vin'][0]['value'] == parent_value / Raptoreum.VALUE_PER_COIN
    # Address should be populated
    assert patched['vin'][0].get('address')

    session.daemon_request.assert_awaited_once_with(
        'getrawtransactions', [parent_txid])


@pytest.mark.asyncio
async def test_fix_vin_preserves_existing_address():
    """If the vin already has an address, don't overwrite it."""
    session = _make_session()

    hash160 = bytes(range(20))
    pk_script = _p2pkh_script(hash160)
    parent_value = 100_000_000

    parent_raw = _build_raw_tx([
        TxOutput(value=parent_value, pk_script=pk_script),
    ])
    parent_txid = 'cc' * 32
    existing_addr = 'rKeepThisAddress'

    result = {
        'vin': [
            {
                'txid': parent_txid, 'vout': 0,
                'address': existing_addr, 'sequence': 0xffffffff,
            },
        ],
        'vout': [],
    }

    session.daemon_request = mock.AsyncMock(return_value=[parent_raw])

    patched = await session._fix_vin_values(result)
    assert patched['vin'][0]['valueSat'] == parent_value
    assert patched['vin'][0]['address'] == existing_addr


@pytest.mark.asyncio
async def test_fix_vin_multiple_inputs_same_parent():
    """Multiple vin entries referencing the same parent tx."""
    session = _make_session()

    hash160_a = bytes(range(20))
    hash160_b = bytes(range(1, 21))
    outputs = [
        TxOutput(value=1_000_000, pk_script=_p2pkh_script(hash160_a)),
        TxOutput(value=2_000_000, pk_script=_p2pkh_script(hash160_b)),
    ]
    parent_raw = _build_raw_tx(outputs)
    parent_txid = 'dd' * 32

    result = {
        'vin': [
            {'txid': parent_txid, 'vout': 0, 'sequence': 0xffffffff},
            {'txid': parent_txid, 'vout': 1, 'sequence': 0xffffffff},
        ],
        'vout': [],
    }

    session.daemon_request = mock.AsyncMock(return_value=[parent_raw])

    patched = await session._fix_vin_values(result)
    assert patched['vin'][0]['valueSat'] == 1_000_000
    assert patched['vin'][1]['valueSat'] == 2_000_000
    # Only one batch call for the same parent
    session.daemon_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_fix_vin_missing_parent_skipped():
    """If a parent tx cannot be fetched, the vin entry stays unchanged."""
    session = _make_session()

    result = {
        'vin': [
            {'txid': 'ee' * 32, 'vout': 0, 'sequence': 0xffffffff},
        ],
        'vout': [],
    }

    session.daemon_request = mock.AsyncMock(return_value=[None])

    patched = await session._fix_vin_values(result)
    assert 'valueSat' not in patched['vin'][0]


@pytest.mark.asyncio
async def test_fix_vin_enriches_missing_valueSat():
    """Vin entries without valueSat trigger enrichment."""
    session = _make_session()

    hash160 = bytes(range(20))
    pk_script = _p2pkh_script(hash160)
    parent_value = 50_000_000
    parent_raw = _build_raw_tx([
        TxOutput(value=parent_value, pk_script=pk_script),
    ])
    parent_txid = 'ff' * 32

    result = {
        'vin': [
            {'txid': parent_txid, 'vout': 0, 'sequence': 0xffffffff},
        ],
        'vout': [],
    }

    session.daemon_request = mock.AsyncMock(return_value=[parent_raw])

    patched = await session._fix_vin_values(result)
    assert patched['vin'][0]['valueSat'] == parent_value
    assert patched['vin'][0]['value'] == parent_value / Raptoreum.VALUE_PER_COIN
    session.daemon_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_transaction_get_non_verbose_passthrough():
    """Non-verbose calls should pass through without enrichment."""
    session = _make_session()

    raw_hex = 'cafebabe'
    # Mock super().transaction_get to return a raw hex string
    parent_cls_mock = mock.AsyncMock(return_value=raw_hex)
    with mock.patch.object(
        RaptoreumElectrumX.__bases__[0], 'transaction_get', parent_cls_mock
    ):
        # Rebind after patching
        session.transaction_get = (
            RaptoreumElectrumX.transaction_get.__get__(session))
        result = await session.transaction_get('aa' * 32, False)

    assert result == raw_hex
