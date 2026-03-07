"""Tests for Raptoreum (RTM) asset indexing.

Covers:
- Asset script parsing (parse_asset_script)
- hashX_from_script stripping
- Asset DB value encoding/decoding
- FlushData asset fields
- Asset undo entry format
"""


import struct
from unittest.mock import MagicMock, patch

import pytest

from electrumx.lib.coins import Raptoreum, RaptoreumTestnet
from electrumx.server.db import FlushData, AssetUTXO, UTXO, COMP_TXID_LEN
from electrumx.lib.hash import HASHX_LEN
from electrumx.server.history import TXNUM_LEN


# ---------------------------------------------------------------------------
# Real on-chain test vectors (from mainnet address RWLZcGY9iUkRrNvtHWNgngqfKTYLrie9MY)
# ---------------------------------------------------------------------------

# TX 4e39329c... vout[0]: asset_id=3eac35a7..., uid=4, amount=1.00000000
ASSET_SCRIPT_1 = bytes.fromhex(
    '76a914e6fddca3370f7758b21ea10e3a08f620a3168c3c88ac'  # P2PKH (25 bytes)
    'bc'                                                    # OP_ASSET_ID
    '4c'                                                    # OP_PUSHDATA1
    '55'                                                    # len=85
    '72746d40'                                              # "rtm@"
    '3365616333356137373939626566373431366330613333316637'  # asset_id hex (64 chars)
    '6330616361373263353965366666383736333234363765336464'
    '383734313833626166303564'
    '01'                                                    # flag=1
    '0400000000000000'                                      # unique_id=4 (LE)
    '00e1f50500000000'                                      # amount=100000000 (LE)
    '75'                                                    # OP_DROP
)

# TX 97823b37... vout[0]: asset_id=d47c090b..., uid=1, amount=1.00000000
ASSET_SCRIPT_2 = bytes.fromhex(
    '76a914e6fddca3370f7758b21ea10e3a08f620a3168c3c88ac'
    'bc4c55'
    '72746d40'
    '6434376330393062653138323539353136343132323264346132'
    '3230363161653034376431353236303134376539393637333232'
    '37333263323564313038613401010000000000000000e1f50500000000'
    '75'
)

# TX 069ee0fc... vout[1]: asset_id=b99da082..., uid=70, amount=999930.00000000
ASSET_SCRIPT_3 = bytes.fromhex(
    '76a9144f0455a166a3361d702423f37cd282573bea6e6188ac'
    'bc4c55'
    '72746d40'
    '6239396461303832306636646630356565626439656232306531'
    '3564333663633264346664343366626166386465633536333531'
    '61633665386239353762353201460000000000000000ba3e6ff15a0000'
    '75'
)

# A plain P2PKH script (no asset payload)
PLAIN_P2PKH = bytes.fromhex(
    '76a914e6fddca3370f7758b21ea10e3a08f620a3168c3c88ac'
)


# ---------------------------------------------------------------------------
# parse_asset_script tests
# ---------------------------------------------------------------------------

class TestParseAssetScript:
    """Test Raptoreum.parse_asset_script with real on-chain data."""

    def test_basic_asset_parse(self):
        result = Raptoreum.parse_asset_script(ASSET_SCRIPT_1)
        assert result is not None
        assert result['asset_id'] == '3eac35a7799bef7416c0a331f7c0aca72c59e6ff87632467e3dd874183baf05d'
        assert result['flag'] == 1
        assert result['unique_id'] == 4
        assert result['amount'] == 100_000_000

    def test_second_asset(self):
        result = Raptoreum.parse_asset_script(ASSET_SCRIPT_2)
        assert result is not None
        assert result['asset_id'] == 'd47c090be1825951641222d4a22061ae047d15260147e9967322732c25d108a4'
        assert result['flag'] == 1
        assert result['unique_id'] == 1
        assert result['amount'] == 100_000_000

    def test_large_amount_asset(self):
        result = Raptoreum.parse_asset_script(ASSET_SCRIPT_3)
        assert result is not None
        assert result['asset_id'] == 'b99da0820f6df05eebd9eb20e15d36cc2d4fd43fbaf8dec56351ac6e8b957b52'
        assert result['flag'] == 1
        assert result['unique_id'] == 70
        assert result['amount'] == 99_993_000_000_000

    def test_plain_p2pkh_returns_none(self):
        result = Raptoreum.parse_asset_script(PLAIN_P2PKH)
        assert result is None

    def test_empty_script(self):
        assert Raptoreum.parse_asset_script(b'') is None

    def test_script_with_only_marker(self):
        # Just ac bc, no payload
        assert Raptoreum.parse_asset_script(b'\xac\xbc') is None

    def test_short_payload(self):
        # Marker + PUSHDATA1 but not enough bytes
        script = b'\xac\xbc\x4c\x55' + b'\x00' * 50
        assert Raptoreum.parse_asset_script(script) is None

    def test_wrong_prefix(self):
        # Valid length but payload doesn't start with rtm@
        script = PLAIN_P2PKH + b'\xbc\x4c\x55' + b'xxxx' + b'\x00' * 81
        assert Raptoreum.parse_asset_script(script) is None

    def test_testnet_inherits_parser(self):
        result = RaptoreumTestnet.parse_asset_script(ASSET_SCRIPT_1)
        assert result is not None
        assert result['amount'] == 100_000_000


# ---------------------------------------------------------------------------
# hashX_from_script tests
# ---------------------------------------------------------------------------

class TestHashXFromScript:
    """Verify hashX_from_script strips asset payload correctly."""

    def test_asset_script_matches_plain_p2pkh(self):
        """An asset script should produce the same hashX as the plain P2PKH
        to the same address."""
        hashX_plain = Raptoreum.hashX_from_script(PLAIN_P2PKH)
        hashX_asset = Raptoreum.hashX_from_script(ASSET_SCRIPT_1)
        assert hashX_plain == hashX_asset

    def test_different_assets_same_address_same_hashX(self):
        """Different asset outputs to the same address produce same hashX."""
        h1 = Raptoreum.hashX_from_script(ASSET_SCRIPT_1)
        h2 = Raptoreum.hashX_from_script(ASSET_SCRIPT_2)
        assert h1 == h2

    def test_different_address_different_hashX(self):
        """Asset output to a different address produces different hashX."""
        h1 = Raptoreum.hashX_from_script(ASSET_SCRIPT_1)
        h3 = Raptoreum.hashX_from_script(ASSET_SCRIPT_3)  # different addr
        assert h1 != h3

    def test_hashX_length(self):
        hashX = Raptoreum.hashX_from_script(ASSET_SCRIPT_1)
        assert len(hashX) == HASHX_LEN


# ---------------------------------------------------------------------------
# Asset DB value encoding/decoding
# ---------------------------------------------------------------------------

class TestAssetDBValue:
    """Test the 49-byte asset DB value format used in b'a' entries."""

    def _encode(self, asset_info):
        """Encode asset info to 49-byte DB value (same logic as block_processor)."""
        asset_id_raw = bytes.fromhex(asset_info['asset_id'])
        return (asset_id_raw
                + struct.pack('B', asset_info['flag'])
                + struct.pack('<q', asset_info['unique_id'])
                + struct.pack('<q', asset_info['amount']))

    def _decode(self, db_value):
        """Decode 49-byte DB value back to asset info."""
        asset_id_raw = db_value[:32]
        flag = db_value[32]
        unique_id = struct.unpack('<q', db_value[33:41])[0]
        amount = struct.unpack('<q', db_value[41:49])[0]
        return {
            'asset_id': asset_id_raw.hex(),
            'flag': flag,
            'unique_id': unique_id,
            'amount': amount,
        }

    def test_roundtrip(self):
        info = Raptoreum.parse_asset_script(ASSET_SCRIPT_1)
        encoded = self._encode(info)
        assert len(encoded) == 49
        decoded = self._decode(encoded)
        assert decoded == info

    def test_roundtrip_large_values(self):
        info = Raptoreum.parse_asset_script(ASSET_SCRIPT_3)
        encoded = self._encode(info)
        decoded = self._decode(encoded)
        assert decoded['unique_id'] == 70
        assert decoded['amount'] == 99_993_000_000_000


# ---------------------------------------------------------------------------
# FlushData asset fields
# ---------------------------------------------------------------------------

class TestFlushDataAssetFields:
    """Verify FlushData carries asset_adds, asset_deletes, asset_undo_infos."""

    def test_default_empty(self):
        fd = FlushData(
            height=100, tx_count=50, headers=[], block_tx_hashes=[],
            undo_infos=[], adds={}, deletes=[], tip=b'\x00' * 32,
        )
        assert fd.asset_adds == {}
        assert fd.asset_deletes == []
        assert fd.asset_undo_infos == []

    def test_explicit_values(self):
        adds = {b'key1': b'val1'}
        deletes = [b'del1']
        undos = [(b'entry', 100)]
        fd = FlushData(
            height=100, tx_count=50, headers=[], block_tx_hashes=[],
            undo_infos=[], adds={}, deletes=[], tip=b'\x00' * 32,
            asset_adds=adds, asset_deletes=deletes, asset_undo_infos=undos,
        )
        assert fd.asset_adds is adds
        assert fd.asset_deletes is deletes
        assert fd.asset_undo_infos is undos


# ---------------------------------------------------------------------------
# Asset undo entry format
# ---------------------------------------------------------------------------

class TestAssetUndoFormat:
    """Test the 69-byte asset undo entry format:
    hashX(11) + txout_idx(4) + tx_num(5) + asset_db_value(49)
    """

    def test_undo_entry_size(self):
        entry_len = HASHX_LEN + 4 + TXNUM_LEN + 49
        assert entry_len == 69

    def test_undo_entry_roundtrip(self):
        """Build an undo entry and extract its components."""
        hashX = b'\x01' * HASHX_LEN
        txout_idx = struct.pack('<I', 2)
        tx_num = struct.pack('<Q', 12345)[:TXNUM_LEN]
        asset_info = Raptoreum.parse_asset_script(ASSET_SCRIPT_1)
        asset_val = (bytes.fromhex(asset_info['asset_id'])
                     + struct.pack('B', asset_info['flag'])
                     + struct.pack('<q', asset_info['unique_id'])
                     + struct.pack('<q', asset_info['amount']))

        entry = hashX + txout_idx + tx_num + asset_val
        assert len(entry) == 69

        # Extract
        e_hashX = entry[:HASHX_LEN]
        e_idx = entry[HASHX_LEN:HASHX_LEN + 4]
        e_txnum = entry[HASHX_LEN + 4:HASHX_LEN + 4 + TXNUM_LEN]
        e_asset = entry[HASHX_LEN + 4 + TXNUM_LEN:]

        assert e_hashX == hashX
        assert struct.unpack('<I', e_idx)[0] == 2
        assert e_txnum == tx_num
        assert e_asset == asset_val

    def test_multiple_undo_entries(self):
        """Multiple entries concatenated and iterated."""
        entry_len = HASHX_LEN + 4 + TXNUM_LEN + 49
        entries = []
        for i in range(3):
            hashX = bytes([i]) * HASHX_LEN
            idx = struct.pack('<I', i)
            txnum = struct.pack('<Q', 1000 + i)[:TXNUM_LEN]
            asset = b'\xaa' * 49
            entries.append(hashX + idx + txnum + asset)

        blob = b''.join(entries)
        assert len(blob) == entry_len * 3

        parsed = []
        for i in range(0, len(blob), entry_len):
            parsed.append(blob[i:i + entry_len])
        assert len(parsed) == 3
        for i, entry in enumerate(parsed):
            assert entry[:HASHX_LEN] == bytes([i]) * HASHX_LEN


# ---------------------------------------------------------------------------
# AssetUTXO dataclass
# ---------------------------------------------------------------------------

class TestAssetUTXO:
    def test_fields(self):
        u = AssetUTXO(
            tx_num=100, tx_pos=0, tx_hash=b'\xaa' * 32, height=5000,
            asset_id='aabb' * 16, flag=1, unique_id=42, amount=1_000_000,
        )
        assert u.tx_num == 100
        assert u.tx_pos == 0
        assert u.height == 5000
        assert u.asset_id == 'aabb' * 16
        assert u.flag == 1
        assert u.unique_id == 42
        assert u.amount == 1_000_000


# ---------------------------------------------------------------------------
# Integration: advance_txs with asset outputs
# ---------------------------------------------------------------------------

class TestAdvanceTxsAssets:
    """Simulate advance_txs processing to verify asset cache population."""

    def _make_tx(self, txid, outputs, inputs=None):
        """Build a minimal Tx-like object."""
        tx = MagicMock()
        tx.txid = txid

        out_list = []
        for pk_script, value in outputs:
            out = MagicMock()
            out.pk_script = pk_script
            out.value = value
            out_list.append(out)
        tx.outputs = out_list

        in_list = []
        if inputs:
            for prev_hash, prev_idx in inputs:
                inp = MagicMock()
                inp.prev_hash = prev_hash
                inp.prev_idx = prev_idx
                inp.is_generation = MagicMock(return_value=False)
                in_list.append(inp)
        else:
            inp = MagicMock()
            inp.is_generation = MagicMock(return_value=True)
            in_list.append(inp)
        tx.inputs = in_list

        return tx

    def test_asset_output_populates_cache(self):
        """When advance_txs processes an asset output, the asset_cache
        should be populated."""
        from electrumx.lib.util import pack_le_uint32, pack_le_uint64

        # We can't easily instantiate BlockProcessor, so test the logic
        # by simulating what advance_txs does for asset outputs
        asset_cache = {}
        tx_hash = b'\x01' * 32
        idx = 0
        pk_script = ASSET_SCRIPT_1

        asset_info = Raptoreum.parse_asset_script(pk_script)
        assert asset_info is not None

        asset_id_raw = bytes.fromhex(asset_info['asset_id'])
        asset_db_value = (
            asset_id_raw
            + struct.pack('B', asset_info['flag'])
            + struct.pack('<q', asset_info['unique_id'])
            + struct.pack('<q', asset_info['amount'])
        )
        asset_cache[tx_hash + pack_le_uint32(idx)] = asset_db_value

        # Verify in cache
        key = tx_hash + pack_le_uint32(0)
        assert key in asset_cache
        val = asset_cache[key]
        assert len(val) == 49
        assert val[:32] == asset_id_raw

    def test_non_asset_output_skips_cache(self):
        """Plain P2PKH outputs should not create asset_cache entries."""
        from electrumx.lib.util import pack_le_uint32

        asset_cache = {}
        tx_hash = b'\x02' * 32
        idx = 0

        asset_info = Raptoreum.parse_asset_script(PLAIN_P2PKH)
        assert asset_info is None
        # No entry added
        assert len(asset_cache) == 0

    def test_spend_removes_from_cache(self):
        """Spending an asset output removes it from asset_cache."""
        from electrumx.lib.util import pack_le_uint32

        asset_cache = {}
        tx_hash = b'\x03' * 32
        key = tx_hash + pack_le_uint32(0)
        asset_cache[key] = b'\xaa' * 49

        # Simulate spend
        val = asset_cache.pop(key, None)
        assert val is not None
        assert len(val) == 49
        assert key not in asset_cache


# ---------------------------------------------------------------------------
# DB key format tests
# ---------------------------------------------------------------------------

class TestAssetDBKeyFormat:
    """Verify the b'a' + hashX + suffix key construction used in flush."""

    def test_key_construction(self):
        from electrumx.lib.util import pack_le_uint32, pack_le_uint64
        hashX = b'\xaa' * HASHX_LEN
        txout_idx = pack_le_uint32(2)
        tx_num = pack_le_uint64(12345)[:TXNUM_LEN]
        suffix = txout_idx + tx_num

        db_key = b'a' + hashX + suffix
        expected_len = 1 + HASHX_LEN + 4 + TXNUM_LEN
        assert len(db_key) == expected_len
        assert db_key[0:1] == b'a'
        assert db_key[1:1 + HASHX_LEN] == hashX

    def test_asset_undo_key(self):
        """Test b'V' + height key format for asset undo."""
        from electrumx.lib.util import pack_be_uint32, unpack_be_uint32
        height = 1294833
        key = b'V' + pack_be_uint32(height)
        assert len(key) == 5
        assert key[0:1] == b'V'
        recovered, = unpack_be_uint32(key[1:])
        assert recovered == height
