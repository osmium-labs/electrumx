# Running the test suite

## 1. System prerequisite (LevelDB)

`plyvel` (a core dependency) is a C extension that needs the LevelDB headers at
build time. pip cannot install these — use your OS package manager:

```bash
# Debian / Ubuntu
sudo apt-get install -y libleveldb-dev
# Fedora / RHEL
sudo dnf install -y leveldb-devel
# macOS (Homebrew)
brew install leveldb
```

## 2. Install the package with test dependencies

From the repo root, in your virtualenv:

```bash
pip install -e '.[test]'
```

The `test` extra pulls in `pytest` + `pytest-asyncio` and the altcoin
header-hash libraries the block/address tests exercise (`blake256`,
`groestlcoin-hash`, `dash_hash`, `pycryptodomex`). Raptoreum's hashing is
covered by the core deps; Osmium's PoW hash needs `dash_hash`.

## 3. Run

```bash
pytest
```

Expected: the full suite passes. A handful of tests still **skip** — those
cover other altcoins whose hash libraries aren't installed (e.g.
`tribushashm`, `xevan-hash`, `zny-yespower-0-5`). Each is available as its own
optional extra (see `[project.optional-dependencies]` in `pyproject.toml`) if
you need to exercise that coin specifically; they're not required for the
Raptoreum/Osmium fork.

## Notes

- `test_blocks.py` reads fixtures from `tests/blocks/*.json`
  (`‹coin›_‹net›_‹height›.json`); each must round-trip through the coin's
  deserializer. `test_all_coins_are_covered` enforces that every mainnet coin
  class has at least one fixture.
- `test_compaction.py` opens a real LevelDB, so it needs step 1 above.
