# Running ElectrumX under systemd (multi-instance, self-maintaining)

These units run any number of ElectrumX instances from one templated service
and keep each instance's history database compacted automatically, so it never
hits the flush-count ceiling.

## Why this exists: the flush-count ceiling

Once an instance is caught up, ElectrumX flushes its history roughly **once per
block**, and each flush bumps a counter that is packed into a **2-byte** key
suffix. At block cadence that counter reaches its hard ceiling of **65535**
every couple of months and the server dies on `struct.error: 'H' format
requires 0 <= number <= 65535`. The only reset is **history compaction**, which
merges the per-flush segments and drops the counter back to a small number.

Protection here is two layers:

1. **Proactive (primary):** a weekly `electrumx-compact@<inst>.timer` keeps the
   counter near zero — it never approaches the ceiling.
2. **Self-heal (backstop):** the server compacts automatically at startup if the
   counter is already near the limit (`History.needs_compaction`), and the unit
   uses `Restart=always`, so even a missed timer becomes a brief auto-recovering
   restart instead of a dead service.

## Files

| File | Install to | Purpose |
|------|-----------|---------|
| `electrumx@.service` | `/etc/systemd/system/` | templated server, one per instance |
| `electrumx-compact@.service` | `/etc/systemd/system/` | oneshot: stop → compact → start |
| `electrumx-compact@.timer` | `/etc/systemd/system/` | weekly trigger for the oneshot |
| `electrumx-compact` | `/usr/local/bin/` (chmod +x) | wrapper the oneshot runs |

## Install on a new machine

```bash
# 1. Units + wrapper
sudo cp electrumx@.service electrumx-compact@.service electrumx-compact@.timer \
        /etc/systemd/system/
sudo install -m 0755 electrumx-compact /usr/local/bin/electrumx-compact
sudo systemctl daemon-reload

# 2. One env file per instance (see electrumx.conf for the full field list)
sudo mkdir -p /etc/electrumx
sudo editor /etc/electrumx/rtm.conf          # COIN, NET, DB_DIRECTORY, DAEMON_URL, SERVICES, ...

# 3. (virtualenv installs only) point ExecStart at your venv, once for all instances
sudo systemctl edit electrumx@
#   [Service]
#   ExecStart=
#   ExecStart=/path/to/venv/bin/electrumx_server

# 4. Enable the server AND its compaction timer
sudo systemctl enable --now electrumx@rtm
sudo systemctl enable --now electrumx-compact@rtm.timer
```

Repeat steps 2 & 4 per chain (`osmi`, `rtm-testnet`, …). The instance name is
whatever precedes `.conf`.

The default `electrumx@.service` runs as user `electrumx`. To run as a different
account, edit `User=`/`Group=` (or `systemctl edit electrumx@`) and set
`ELECTRUMX_USER` for the wrapper (drop-in on `electrumx-compact@.service`:
`[Service]` / `Environment=ELECTRUMX_USER=youruser`).

## Migrating from bespoke per-chain units

If you already run hand-written units (`electrumx-rtm.service`, etc.), move each
to the template once:

```bash
# Reuse your existing env file as the per-instance conf
sudo mkdir -p /etc/electrumx
sudo cp /path/to/rtm/electrumx.env /etc/electrumx/rtm.conf

sudo systemctl disable --now electrumx-rtm           # stop the old unit
sudo systemctl enable  --now electrumx@rtm           # start the templated one
sudo systemctl enable  --now electrumx-compact@rtm.timer
```

`DB_DIRECTORY` is unchanged, so no resync — the templated unit opens the same
database. Once verified, delete the old `/etc/systemd/system/electrumx-rtm.service`.

## Operating

```bash
systemctl status electrumx@rtm
journalctl -u electrumx@rtm -f

# Compact on demand (also what the timer runs):
sudo systemctl start electrumx-compact@rtm

# See when each timer next fires:
systemctl list-timers 'electrumx-compact@*'
```
