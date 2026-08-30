'''Spendability on listunspent: is_coinbase, asset, futures lock.

WHAT THIS IS FOR.  ElectrumX offers every unspent output as ordinary money.
Three kinds of output are not, and consensus is the thing that says so:

  - a coinbase under COINBASE_MATURITY confirmations, which cannot be spent;
  - an output carrying an RTM asset, which carries zero RTM and is otherwise
    indistinguishable from dust -- spending one as a fee destroys a holding;
  - the locked output of a DIP2 type-7 futures transaction, which
    validateFutureCoin rejects with bad-txns-premature-spend-of-future.

None of the three was visible on listunspent, so a client had to fetch the
funding transaction of EVERY unspent output to find out -- one round trip
each, and in practice capped somewhere, because a Raptoreum payout address
holding 10,480 of them would be 10,000 round trips to answer one request.

The interesting cases are the ones where getting it wrong offers coins that
cannot move, so these lean on the fail-closed direction.
'''
from unittest import mock

import pytest
from aiorpcx import RPCError  # noqa: F401  (imported for parity with siblings)

from electrumx.lib.coins import Raptoreum
from electrumx.lib.util import pack_le_int32, pack_le_uint16, pack_le_uint64
from electrumx.server.db import (DB, FUTURES_PREFIX, FUTURES_UNREADABLE, AssetUTXO, UTXO)
from electrumx.server.block_processor import pack_futures_lock
from electrumx.server.history import TXNUM_LEN
from electrumx.server.session import ElectrumX, RaptoreumElectrumX

from .test_history_paging import MemoryStorage


# ---------------------------------------------------------------------------
# is_coinbase: derived, not stored
# ---------------------------------------------------------------------------

class CountsOnly:
    '''Just enough DB for the coinbase derivation, which reads only tx_counts.'''

    is_coinbase_utxo = DB.is_coinbase_utxo

    def __init__(self, tx_counts):
        self.tx_counts = tx_counts


# Block 0 holds 1 tx (tx_num 0), block 1 holds 3 (1..3), block 2 holds 2 (4..5).
COUNTS = [1, 4, 6]


@pytest.mark.parametrize('tx_num, height, expected', [
    (1, 1, True),    # first of block 1
    (2, 1, False),   # second of block 1 -- an ordinary transaction
    (3, 1, False),
    (4, 2, True),    # first of block 2
    (5, 2, False),
])
def test_the_first_transaction_of_a_block_is_its_coinbase(tx_num, height, expected):
    assert CountsOnly(COUNTS).is_coinbase_utxo(tx_num, height) is expected


def test_a_mempool_output_is_never_a_coinbase():
    # The mempool builds its UTXOs with height 0.
    assert CountsOnly(COUNTS).is_coinbase_utxo(-1, 0) is False


def test_height_zero_is_not_answered_by_reading_backwards():
    # THE TEST THAT DISTINGUISHES THE GUARD FROM ITS ABSENCE, which the two
    # above do not: with COUNTS = [1, 4, 6], a height-0 output falling through
    # compares tx_num against tx_counts[-1] -- the chain TIP's cumulative count,
    # 6 -- and answers True. Both of those cases answer False either way, so a
    # version with no guard at all passes them.
    #
    # The genesis coinbase is unspendable by consensus and never reaches this
    # set, so False is the whole of the correct answer at height 0.
    assert CountsOnly(COUNTS).is_coinbase_utxo(6, 0) is False
    assert CountsOnly(COUNTS).is_coinbase_utxo(0, 0) is False


def test_a_height_past_the_tip_is_not_guessed():
    assert CountsOnly(COUNTS).is_coinbase_utxo(99, 9) is False


# ---------------------------------------------------------------------------
# The CFutureTx payload
# ---------------------------------------------------------------------------

def payload(version=1, maturity=100, lock_time=-1, lock_idx=1, fee=100):
    return (pack_le_uint16(version) + pack_le_int32(maturity)
            + pack_le_int32(lock_time) + pack_le_uint16(lock_idx)
            + pack_le_uint16(fee))


def test_reads_the_documented_offsets():
    assert Raptoreum.parse_futures_payload(payload(maturity=100, lock_time=-1, lock_idx=1)) \
        == (100, None, 1)


def test_minus_one_disables_a_condition_rather_than_meaning_four_billion():
    # THE ONE THAT MATTERS. Read unsigned, -1 becomes 4,294,967,295 blocks --
    # a lock that never releases, on coins that are actually spendable.
    maturity, lock_time, _idx = Raptoreum.parse_futures_payload(
        payload(maturity=-1, lock_time=1700000000))

    assert maturity is None
    assert lock_time == 1700000000


def test_both_conditions_can_be_set():
    assert Raptoreum.parse_futures_payload(payload(maturity=50, lock_time=99)) == (50, 99, 1)


def test_the_locked_output_index_is_read_not_assumed():
    # Only lockOutputIndex is locked; the change output of the same
    # transaction spends normally. Assuming output 0 would strand the change
    # and release the lock.
    assert Raptoreum.parse_futures_payload(payload(lock_idx=3))[2] == 3


@pytest.mark.parametrize('raw', [None, b'', payload()[:11]])
def test_a_payload_too_short_to_name_an_output_is_refused(raw):
    # Refused rather than guessed. The caller records the refusal, which
    # withholds every output of the transaction -- the safe direction.
    assert Raptoreum.parse_futures_payload(raw) is None


def test_a_longer_payload_still_parses():
    # A future version may append fields. The offsets under test are all in the
    # first twelve bytes, and refusing on extra bytes would break on an upgrade
    # that did not change anything this reads.
    assert Raptoreum.parse_futures_payload(payload() + b'\x00' * 16) == (100, None, 1)


# ---------------------------------------------------------------------------
# The futures index
# ---------------------------------------------------------------------------

class FuturesDB:
    '''DB with only what the futures reader and its reorg cleanup touch.'''

    futures_locks = DB.futures_locks
    delete_futures_from = DB.delete_futures_from

    def __init__(self):
        self.utxo_db = MemoryStorage()

    def write(self, tx_num, maturity, lock_time, lock_idx):
        self.utxo_db.put(
            FUTURES_PREFIX + pack_le_uint64(tx_num)[:TXNUM_LEN],
            pack_le_int32(maturity) + pack_le_int32(lock_time) + pack_le_uint16(lock_idx))


@pytest.mark.asyncio
async def test_a_written_lock_reads_back():
    db = FuturesDB()
    db.write(41, 100, -1, 2)

    assert await db.futures_locks([41]) == {41: (100, None, 2)}


@pytest.mark.asyncio
async def test_a_transaction_with_no_lock_is_simply_absent():
    # Absent rather than a None entry: the caller iterates what came back, and
    # a placeholder would have every ordinary transaction in the map.
    db = FuturesDB()
    db.write(41, 100, -1, 2)

    assert await db.futures_locks([7, 41, 99]) == {41: (100, None, 2)}


@pytest.mark.asyncio
async def test_mempool_tx_nums_are_not_looked_up():
    assert await FuturesDB().futures_locks([-1]) == {}


@pytest.mark.asyncio
async def test_a_reorg_drops_records_above_the_new_tip():
    # THE REASON THIS IS DELETED AT ALL. tx_nums at or above the new tip are
    # handed to whatever transactions land there next; a stale record would
    # attach somebody's lock to an unrelated output and withhold coins that
    # were never locked.
    db = FuturesDB()
    for tx_num in (10, 20, 30):
        db.write(tx_num, 100, -1, 0)

    removed = db.delete_futures_from(20)

    assert removed == 2
    assert await db.futures_locks([10, 20, 30]) == {10: (100, None, 0)}


def test_a_reorg_that_drops_nothing_reports_nothing():
    db = FuturesDB()
    db.write(10, 100, -1, 0)

    assert db.delete_futures_from(999) == 0


# ---------------------------------------------------------------------------
# listunspent
# ---------------------------------------------------------------------------

TXID = bytes.fromhex('ab' * 32)
TXID_HEX = 'ab' * 32
OTHER = bytes.fromhex('cd' * 32)
OTHER_HEX = 'cd' * 32


def _session(cls, utxos, tx_counts=COUNTS):
    '''A real instance with its __init__ skipped.

    Not a Mock: RaptoreumElectrumX.hashX_listunspent calls super(), which needs
    an object that genuinely is one -- and a double that let super() through
    would be testing the double's MRO rather than the server's.
    '''
    session = object.__new__(cls)
    session.db = mock.Mock()
    session.db.all_utxos = mock.AsyncMock(return_value=list(utxos))
    session.db.tx_counts = tx_counts
    session.db.is_coinbase_utxo = DB.is_coinbase_utxo.__get__(session.db)
    session.mempool = mock.AsyncMock()
    session.mempool.unordered_UTXOs = mock.AsyncMock(return_value=[])
    session.mempool.potential_spends = mock.AsyncMock(return_value=set())
    session.bump_cost = mock.Mock()

    return session


def _base_session(utxos, tx_counts=COUNTS):
    return _session(ElectrumX, utxos, tx_counts)


@pytest.mark.asyncio
async def test_listunspent_says_which_outputs_are_coinbase():
    session = _base_session([UTXO(1, 0, TXID, 1, 5000),
                             UTXO(2, 0, OTHER, 1, 6000)])

    entries = await session.hashX_listunspent(b'\x01' * 11)

    assert [e['is_coinbase'] for e in entries] == [True, False]


@pytest.mark.asyncio
async def test_listunspent_keeps_the_fields_every_client_already_reads():
    # Additive, so existing Electrum clients keep working. Removing one of
    # these is what would break them.
    session = _base_session([UTXO(2, 3, TXID, 1, 5000)])

    entry = (await session.hashX_listunspent(b'\x01' * 11))[0]

    assert entry['tx_hash'] == TXID_HEX
    assert entry['tx_pos'] == 3
    assert entry['height'] == 1
    assert entry['value'] == 5000


def _rtm_session(utxos, assets=(), locks=None, names=None):
    session = _session(RaptoreumElectrumX, utxos)
    session.db.all_asset_utxos = mock.AsyncMock(return_value=list(assets))
    session.db.futures_locks = mock.AsyncMock(return_value=dict(locks or {}))
    session._asset_metadata = mock.AsyncMock(return_value=dict(names or {}))

    return session


@pytest.mark.asyncio
async def test_an_asset_bearing_output_is_named_as_one():
    # An asset output carries ZERO RTM, so on value alone it looks like an
    # output not worth spending rather than one that must not be.
    asset = AssetUTXO(1, 0, TXID, 1, 'ef' * 32, 0, 0, 250_00000000)
    session = _rtm_session([UTXO(1, 0, TXID, 1, 0)], assets=[asset],
                           names={'ef' * 32: {'name': 'MAGAFISH', 'decimalPoint': 0}})

    entry = (await session.hashX_listunspent(b'\x01' * 11))[0]

    assert entry['asset']['asset_id'] == 'ef' * 32
    assert entry['asset']['name'] == 'MAGAFISH'
    assert entry['asset']['amount'] == 250_00000000


@pytest.mark.asyncio
async def test_an_ordinary_output_carries_no_asset_key():
    # Absent rather than null: a client checking `if entry.asset` must not have
    # to know the difference, and `asset: null` on every ordinary output is
    # bytes on the wire for nothing.
    session = _rtm_session([UTXO(2, 0, TXID, 1, 5000)])

    assert 'asset' not in (await session.hashX_listunspent(b'\x01' * 11))[0]


@pytest.mark.asyncio
async def test_only_the_locked_output_of_a_futures_transaction_is_locked():
    # The change output of the same transaction spends normally. Marking both
    # would strand coins the network would happily move.
    session = _rtm_session([UTXO(2, 0, TXID, 1, 5000), UTXO(2, 1, TXID, 1, 6000)],
                           locks={2: (100, None, 1)})

    entries = await session.hashX_listunspent(b'\x01' * 11)

    assert 'lock' not in entries[0]
    assert entries[1]['lock'] == {'maturity': 100, 'locktime': None, 'readable': True}


@pytest.mark.asyncio
async def test_an_unreadable_payload_locks_every_output_of_its_transaction():
    # THE FAIL-CLOSED CASE. We cannot say which output the lock names, so
    # offering any of them is a guess about coins consensus may refuse to move.
    session = _rtm_session([UTXO(2, 0, TXID, 1, 5000), UTXO(2, 1, TXID, 1, 6000)],
                           locks={2: (None, None, FUTURES_UNREADABLE)})

    entries = await session.hashX_listunspent(b'\x01' * 11)

    assert [e['lock']['readable'] for e in entries] == [False, False]


@pytest.mark.asyncio
async def test_an_output_of_an_ordinary_transaction_carries_no_lock():
    session = _rtm_session([UTXO(2, 0, TXID, 1, 5000)], locks={41: (100, None, 0)})

    assert 'lock' not in (await session.hashX_listunspent(b'\x01' * 11))[0]


@pytest.mark.asyncio
async def test_the_raptoreum_entries_still_carry_is_coinbase():
    # The override must not lose what the base class added; it is the flag that
    # applies to every chain, and it is the one an override is most likely to
    # drop by rebuilding the dicts rather than adding to them.
    session = _rtm_session([UTXO(1, 0, TXID, 1, 5000)])

    assert (await session.hashX_listunspent(b'\x01' * 11))[0]['is_coinbase'] is True


# ---------------------------------------------------------------------------
# What the block processor records
# ---------------------------------------------------------------------------

class FakeTx:
    def __init__(self, tx_type=0, extra_payload=None):
        self.tx_type = tx_type
        self.extra_payload = extra_payload


class NoFutures:
    '''A coin that has never heard of a futures transaction -- every coin but RTM.'''


def test_an_ordinary_transaction_records_nothing():
    assert pack_futures_lock(Raptoreum, FakeTx(tx_type=0, extra_payload=payload())) is None


def test_a_coin_without_futures_records_nothing():
    # And does not reach for a parser it does not have. Every non-RTM chain
    # this server runs takes this branch on every transaction of every block.
    assert pack_futures_lock(NoFutures, FakeTx(tx_type=7, extra_payload=payload())) is None


def test_a_futures_transaction_records_the_output_it_locks():
    packed = pack_futures_lock(Raptoreum, FakeTx(tx_type=7, extra_payload=payload(
        maturity=100, lock_time=-1, lock_idx=2)))

    assert packed == pack_le_int32(100) + pack_le_int32(-1) + pack_le_uint16(2)


def test_a_disabled_condition_is_recorded_as_minus_one():
    # The record round-trips through the reader, which turns -1 back into None.
    # Storing 0 would be a lock that released immediately.
    packed = pack_futures_lock(Raptoreum, FakeTx(tx_type=7, extra_payload=payload(
        maturity=-1, lock_time=1700000000, lock_idx=0)))

    assert packed[:4] == pack_le_int32(-1)


def test_an_unreadable_payload_is_recorded_rather_than_skipped():
    # THE FAIL-CLOSED GUARANTEE, and the reason this function was pulled out of
    # advance_txs at all: inlined, a mutation that dropped this branch changed
    # no test. Not recording it offers every output of a type-7 transaction as
    # ordinary money, and consensus will refuse to move one of them.
    packed = pack_futures_lock(Raptoreum, FakeTx(tx_type=7, extra_payload=b'\x00' * 4))

    assert packed is not None
    assert packed[8:] == pack_le_uint16(FUTURES_UNREADABLE)


def test_a_futures_transaction_with_no_payload_at_all_is_still_recorded():
    assert pack_futures_lock(Raptoreum, FakeTx(tx_type=7)) is not None
