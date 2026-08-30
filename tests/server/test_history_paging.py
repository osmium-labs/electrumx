'''Paged history: History.get_txnums_newest_first and DB.limited_history_page.

WHAT THESE ARE ABOUT.  blockchain.scripthash.get_history returns an address's
entire confirmed history on every call.  Serving a wallet's twenty newest rows
for an address with twenty thousand transactions costs twenty thousand reads
from the hashes file and about two megabytes on the wire, every poll -- and
past env.max_send // 99 entries it costs everything, because the whole history
is refused as 'history too large'.

The paged read is the fix, and the two things worth testing are the two things
that are easy to get wrong: reading BACKWARDS across the row boundary (history
is stored in rows of tx_nums keyed by a flush id, so a naive reverse walks the
rows in the right order and each row's contents in the wrong one), and the
CURSOR, which has to be stable when new transactions arrive.

These drive History and the page reader against an in-memory store rather than
LevelDB.  The behaviour under test is entirely in the walk and the arithmetic;
a real DB adds a system package to run a test that would assert the same thing.
'''
from bisect import bisect_right
from os import urandom
from unittest import mock

import pytest
from aiorpcx import RPCError

from electrumx.lib.hash import HASHX_LEN
from electrumx.lib.util import pack_le_uint64
from electrumx.server.db import DB
from electrumx.server.history import History
from electrumx.server.session import MAX_HISTORY_PAGE, ElectrumX

TXNUM_LEN = 5


class MemoryStorage:
    '''The slice of Storage that History uses: an ordered key/value map.

    Keys are compared as bytes, which is what makes the big-endian flush-id
    suffix sort numerically -- the property the reverse walk depends on.
    '''

    def __init__(self):
        self.items = {}
        self.for_sync = False

    def get(self, key):
        return self.items.get(key)

    def put(self, key, value):
        self.items[key] = value

    def iterator(self, prefix=b'', reverse=False):
        keys = sorted(k for k in self.items if k.startswith(prefix))
        if reverse:
            keys.reverse()

        return ((k, self.items[k]) for k in keys)

    def write_batch(self):
        return _MemoryBatch(self)


class _MemoryBatch:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def put(self, key, value):
        self.store.items[key] = value

    def delete(self, key):
        self.store.items.pop(key, None)


class FakeDB:
    '''DB.limited_history_page needs a history, fs_tx_hash and a logger.

    fs_tx_hash maps a tx_num to (hash, height).  The real one reads 32 bytes
    from the hashes file; this one fabricates a distinguishable hash so a test
    can tell entries apart, and returns None past `known` so the reorg retry
    can be exercised.
    '''

    limited_history_page = DB.limited_history_page

    def __init__(self, history, known=None):
        self.history = history
        self.known = known
        self.logger = _NullLogger()
        self.missing_reads = 0

    def fs_tx_hash(self, tx_num):
        if self.known is not None and tx_num in self.known:
            self.missing_reads += 1
            self.known = None  # heal on the retry, as a settled reorg does

            return None, 0

        return tx_num.to_bytes(32, 'big'), bisect_right([1_000_000], tx_num)


class _NullLogger:
    def warning(self, *_args, **_kwargs):
        pass


def make_history():
    history = History()
    history.db = MemoryStorage()

    return history


def write_history(history, hashX, tx_nums, *, flush_every=None):
    '''Put `tx_nums` in the history of `hashX`, flushing every `flush_every`.

    Flushing is what creates a new DB row, so this is how a test asks for a
    history that spans several rows -- the case a reverse read gets wrong by
    walking rows backwards but their contents forwards.
    '''
    for index, tx_num in enumerate(tx_nums, start=1):
        history.unflushed[hashX] += pack_le_uint64(tx_num)[:TXNUM_LEN]
        history.unflushed_count += 1
        if flush_every and index % flush_every == 0:
            history.flush()
    if history.unflushed:
        history.flush()


def test_yields_newest_first():
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(10))

    assert list(history.get_txnums_newest_first(hashX, limit=None)) == list(range(9, -1, -1))


def test_reads_backwards_across_row_boundaries():
    # THE ONE THAT CATCHES THE OBVIOUS BUG.  Five flushes make five rows; a
    # reverse iterator visits the rows newest-first but each row's tx_nums are
    # appended in increasing order, so a read that reverses only the rows
    # returns [16..19, 12..15, 8..11, ...] -- sorted in neither direction, and
    # indistinguishable from correct on a history small enough to fit one row.
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(20), flush_every=4)

    assert list(history.get_txnums_newest_first(hashX, limit=None)) == list(range(19, -1, -1))


def test_limit_stops_the_walk():
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(1000), flush_every=50)

    assert list(history.get_txnums_newest_first(hashX, limit=3)) == [999, 998, 997]


def test_before_excludes_its_own_entry():
    # `before` is exclusive, which is what makes the cursor chain non-repeating:
    # the cursor IS the last entry of the page just served, so an inclusive
    # bound would show it again at the top of the next one.
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(10))

    assert list(history.get_txnums_newest_first(hashX, limit=3, before=7)) == [6, 5, 4]


def test_before_skips_without_resolving():
    # The reason `before` is applied inside the walk rather than by the caller.
    # A skipped tx_num costs one unpack here; resolving it costs a read from
    # the hashes file, and deep paging skips a page's worth per page.
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(500), flush_every=25)

    assert list(history.get_txnums_newest_first(hashX, limit=2, before=100)) == [99, 98]


def test_unknown_hashX_is_empty_not_an_error():
    history = make_history()
    write_history(history, urandom(HASHX_LEN), range(5))

    assert list(history.get_txnums_newest_first(urandom(HASHX_LEN), limit=10)) == []


def test_other_addresses_are_not_walked():
    # The prefix is the hashX, so a neighbouring address's rows sort adjacent
    # to this one's. A reverse iterator that lost the prefix would start on
    # somebody else's history and return it as this address's.
    history = make_history()
    mine, theirs = b'\x01' * HASHX_LEN, b'\x02' * HASHX_LEN
    write_history(history, mine, range(5))
    write_history(history, theirs, range(100, 105))

    assert list(history.get_txnums_newest_first(mine, limit=None)) == [4, 3, 2, 1, 0]


@pytest.mark.asyncio
async def test_page_reports_a_cursor_only_when_there_is_more():
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(10))
    db = FakeDB(history)

    _, cursor = await db.limited_history_page(hashX, limit=4)
    assert cursor == 6

    # Exactly as many entries as remain: there is no next page, so no cursor.
    # Reading limit+1 entries to detect this is why the last page does not cost
    # an extra round trip that comes back empty.
    _, cursor = await db.limited_history_page(hashX, limit=6, before=6)
    assert cursor is None

    _, cursor = await db.limited_history_page(hashX, limit=100)
    assert cursor is None


@pytest.mark.asyncio
async def test_a_page_returns_no_more_than_asked_for():
    # limit+1 is read; returning it would hand the client one row it did not
    # ask for and, worse, one that the next page also returns.
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(50))

    page, _ = await FakeDB(history).limited_history_page(hashX, limit=7)

    assert len(page) == 7


@pytest.mark.asyncio
async def test_the_cursor_walks_the_whole_history_exactly_once():
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(37), flush_every=6)
    db = FakeDB(history)

    seen = []
    cursor = None
    while True:
        page, cursor = await db.limited_history_page(hashX, limit=5, before=cursor)
        seen.extend(page)
        if cursor is None:
            break

    # Every entry once, newest first, and the loop terminated. A cursor that
    # was inclusive duplicates one entry per page and never converges.
    assert len(seen) == 37
    assert len({tx_hash for tx_hash, _height in seen}) == 37


@pytest.mark.asyncio
async def test_a_cursor_survives_new_transactions():
    # THE REASON THE CURSOR IS A tx_num AND NOT AN OFFSET.  Between a wallet
    # asking for page one and asking for page two, blocks arrive and the
    # address is touched.  Under an offset, page two starts five entries later
    # than it should: the five newest entries are shown twice and five older
    # ones are never shown at all.
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(20))
    db = FakeDB(history)

    first, cursor = await db.limited_history_page(hashX, limit=5)
    assert cursor == 15

    write_history(history, hashX, range(20, 25))

    second, _ = await db.limited_history_page(hashX, limit=5, before=cursor)

    # The page after tx_num 15 is 14..10 whenever it is asked for, and none of
    # it overlaps what page one returned.
    assert not {h for h, _ in first} & {h for h, _ in second}
    assert [int.from_bytes(h, 'big') for h, _ in second] == [14, 13, 12, 11, 10]


@pytest.mark.asyncio
async def test_a_tx_that_vanished_is_retried_not_returned():
    # fs_tx_hash returns None for a tx_num whose block went away between the
    # walk and the read. Returning it would put a null tx_hash on the page;
    # limited_history retries instead, and so does this.
    history = make_history()
    hashX = urandom(HASHX_LEN)
    write_history(history, hashX, range(10))
    db = FakeDB(history, known={9})

    page, _ = await db.limited_history_page(hashX, limit=3)

    assert db.missing_reads == 1
    assert all(tx_hash is not None for tx_hash, _height in page)


# ---------------------------------------------------------------------------
# The protocol method
# ---------------------------------------------------------------------------

def _make_session(page=(), cursor=None, mempool=()):
    '''An ElectrumX session with only what get_history_page touches.'''
    session = mock.AsyncMock(spec=ElectrumX)
    session.session_mgr = mock.AsyncMock()
    session.session_mgr.limited_history_page = mock.AsyncMock(
        return_value=(list(page), cursor, 0.1))
    session.unconfirmed_history = mock.AsyncMock(return_value=list(mempool))
    session.bump_cost = mock.Mock()
    session.scripthash_get_history_page = (
        ElectrumX.scripthash_get_history_page.__get__(session))

    return session


SCRIPTHASH = 'ab' * 32
CONFIRMED = [(bytes.fromhex('cd' * 32), 900)]
UNCONFIRMED = [{'tx_hash': 'ef' * 32, 'height': 0, 'fee': 226}]


@pytest.mark.asyncio
async def test_method_returns_history_and_cursor():
    session = _make_session(page=CONFIRMED, cursor=41)

    result = await session.scripthash_get_history_page(SCRIPTHASH, 20)

    assert result['history'] == [{'tx_hash': 'cd' * 32, 'height': 900}]
    # A STRING, not the integer it is derived from. The client is meant to
    # hand it back untouched; typing it as a number invites arithmetic on it,
    # and the next cursor scheme would then be a breaking change.
    assert result['cursor'] == '41'


@pytest.mark.asyncio
async def test_the_last_page_says_so():
    session = _make_session(page=CONFIRMED, cursor=None)

    assert (await session.scripthash_get_history_page(SCRIPTHASH))['cursor'] is None


@pytest.mark.asyncio
async def test_mempool_entries_lead_the_first_page():
    session = _make_session(page=CONFIRMED, mempool=UNCONFIRMED)

    history = (await session.scripthash_get_history_page(SCRIPTHASH))['history']

    # Newest first means unconfirmed first: they are ahead of every mined
    # transaction, and they carry a fee the confirmed entries do not.
    assert history[0]['tx_hash'] == 'ef' * 32
    assert len(history) == 2


@pytest.mark.asyncio
async def test_mempool_entries_do_not_repeat_on_later_pages():
    # THE BUG THIS PREVENTS. The mempool is not paged -- there is no stable
    # order to page it in -- so returning it with every page would show each
    # unconfirmed transaction once per page for as long as it stayed unmined.
    session = _make_session(page=CONFIRMED, mempool=UNCONFIRMED)

    history = (await session.scripthash_get_history_page(SCRIPTHASH, 20, '41'))['history']

    assert [entry['tx_hash'] for entry in history] == ['cd' * 32]
    session.unconfirmed_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_cursor_reaches_the_reader_as_an_exclusive_bound():
    session = _make_session(page=CONFIRMED, cursor=None)

    await session.scripthash_get_history_page(SCRIPTHASH, 20, '41')

    _args, kwargs = session.session_mgr.limited_history_page.call_args
    assert kwargs == {'limit': 20, 'before': 41}


@pytest.mark.asyncio
async def test_no_cursor_means_the_newest_page():
    session = _make_session(page=CONFIRMED, cursor=None)

    await session.scripthash_get_history_page(SCRIPTHASH)

    _args, kwargs = session.session_mgr.limited_history_page.call_args
    assert kwargs['before'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [0, -1, MAX_HISTORY_PAGE + 1])
async def test_a_count_outside_the_bounds_is_refused(count):
    # An unbounded count is get_history with extra steps, and the ceiling this
    # method exists to stay under is env.max_send.
    session = _make_session()

    with pytest.raises(RPCError):
        await session.scripthash_get_history_page(SCRIPTHASH, count)

    session.session_mgr.limited_history_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('cursor', ['not-a-number', '-1', '1.5'])
async def test_a_cursor_that_is_not_a_tx_num_is_refused(cursor):
    # Refused rather than ignored: silently treating a bad cursor as "start
    # from the top" serves page one to a client asking for page nine, which
    # looks like duplicated history rather than like an error.
    session = _make_session()

    with pytest.raises(RPCError):
        await session.scripthash_get_history_page(SCRIPTHASH, 20, cursor)


@pytest.mark.asyncio
async def test_the_method_is_registered_under_its_protocol_name():
    # A method nothing can call is not a feature. set_request_handlers is the
    # only place the name is written down.
    session = mock.Mock(spec=ElectrumX)
    ElectrumX.set_request_handlers(session, (1, 4))

    assert 'blockchain.scripthash.get_history_page' in session.request_handlers
