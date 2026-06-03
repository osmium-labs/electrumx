'''Tests for history flush-count overflow protection.

These exercise the threshold logic only, so they need no DB backend (unlike
test_compaction.py, which opens a real history DB).'''

import struct

import pytest

from electrumx.lib.util import pack_be_uint16
from electrumx.server.history import (
    History, FLUSHID_LEN, MAX_FLUSH_COUNT, COMPACT_FLUSH_COUNT,
)


def test_max_flush_count_matches_flushid_packing():
    # MAX_FLUSH_COUNT must be the largest value flush() can pack into a
    # FLUSHID_LEN-byte suffix; one more must overflow.
    assert MAX_FLUSH_COUNT == (1 << (8 * FLUSHID_LEN)) - 1
    pack_be_uint16(MAX_FLUSH_COUNT)              # must not raise
    with pytest.raises(struct.error):
        pack_be_uint16(MAX_FLUSH_COUNT + 1)


def test_compact_threshold_leaves_margin():
    # The auto-compact threshold must sit below the ceiling with enough
    # headroom to compact on the next restart before overflow.
    assert 0 < COMPACT_FLUSH_COUNT < MAX_FLUSH_COUNT
    assert MAX_FLUSH_COUNT - COMPACT_FLUSH_COUNT >= 1000


def test_needs_compaction_property():
    h = History()
    h.flush_count = COMPACT_FLUSH_COUNT - 1
    assert not h.needs_compaction
    h.flush_count = COMPACT_FLUSH_COUNT
    assert h.needs_compaction
    h.flush_count = MAX_FLUSH_COUNT
    assert h.needs_compaction
