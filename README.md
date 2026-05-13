# Vortex P2P — A 6-Node Blockchain Consensus Demo

> Cloud System midterm project
> M1421070 Hung-Yi Tai · M1429012 Cheng-Han Wu

A from-scratch P2P blockchain demo built with **Python + Flask + Docker + Tailscale**.
No third-party chain, no `web3`, no PoW library — every layer is implemented by hand:
file-based ledger, SHA-256 block chaining, UDP broadcast, heartbeat-driven liveness
detection, dynamic majority consensus, and full network-wide trust freeze /
self-repair after tampering.

---

## Table of Contents

- [Architecture](#architecture)
- [Highlights](#highlights)
- [Project Layout](#project-layout)
- [Quick Start](#quick-start)
- [Web GUI](#web-gui)
- [Demo Walkthrough](#demo-walkthrough)
- [REST API](#rest-api)
- [P2P Message Protocol](#p2p-message-protocol)
- [Ledger File Format](#ledger-file-format)
- [Ledger Integrity Checks](#ledger-integrity-checks)
- [Heartbeat & Live-Node Consensus](#heartbeat--live-node-consensus)
- [Network Trust State (Freeze / Unfreeze)](#network-trust-state-freeze--unfreeze)
- [Consensus & Self-Repair](#consensus--self-repair)
- [FAQ](#faq)

---

## Architecture

The demo runs **6 P2P nodes** spread across two hosts, connected through a
**Tailscale mesh VPN**:

```
        +-------------- Tailscale Network --------------+
        |                                               |
   Host A (100.94.194.29)            Host B (100.122.78.117)
   |-- client1  :8001 / GUI :8081    |-- client1  :8001 / GUI :8081
   |-- client2  :8002 / GUI :8082    |-- client2  :8002 / GUI :8082
   `-- client3  :8003 / GUI :8083    `-- client3  :8003 / GUI :8083

           Node 1 / 2 / 3                  Node 4 / 5 / 6
```

- Each host launches three Ubuntu 22.04 containers via `docker-compose`.
- Containers talk to each other over **UDP 8001-8003** for heartbeat, broadcast,
  and ledger sync.
- Each container also exposes a **Flask Web GUI** (container `:5000` mapped to
  host `:8081/8082/8083`).
- Cross-host traffic always goes over Tailscale IPs — no NAT punching, no public exposure.
- The system **degrades gracefully** when only a subset of the 6 nodes are
  online (minimum quorum: 2 nodes).

---

## Highlights

| Module | Description |
| --- | --- |
| Custom chained ledger | Each `/storage/<n>.txt` is a block (max 5 transactions), linked by SHA-256 |
| Three-layer integrity check | Block-ID contiguity (1..N) + genesis `prev_hash == 0` + per-block hash chain + `latest_hash.txt` head check |
| Reliable UDP transaction broadcast | `TX2` carries before/after ledger hashes; missed or out-of-order packets trigger `REQ_SYNC` repair |
| Heartbeat-driven liveness | `PING` every 2 s, peer goes offline after 5 s of silence; status visible in the GUI |
| Dynamic majority consensus | Threshold computed from **live** peers, not a fixed list — works with any subset ≥ 2 nodes |
| Hard quorum floor | Consensus refuses to run with fewer than 2 live nodes (prevents single-node "self-trust") |
| Token-authenticated voting | Every protocol message carries a shared `network_token`; outside packets are dropped |
| Network-wide trust state | Consensus failure broadcasts `BROADCAST_DISTRUST` so **every** node freezes; success broadcasts `BROADCAST_TRUST` to unfreeze |
| Frozen-node guard | Balance / log / transaction operations refuse to run while frozen, with a clear reason |
| Automatic ledger repair | On hash mismatch, a node sends `REQ_SYNC`, gets the full ledger back, overwrites locally, then reports `REPAIR_DONE` |
| Debounced TCP ledger snapshot | After a burst of transactions, the initiator pushes a final ledger snapshot to live peers to cover the last lost UDP packet |
| Live status panel | Web GUI shows online/offline dots for all 6 nodes, plus a "trusted / frozen" badge |
| One-click demo | `demo.bat` (Windows) and `start_demo.sh` (Linux/macOS) bring everything up and inject 100 random transactions |

---

## Project Layout

```
.
├── Dockerfile                # Ubuntu 22.04 + Python3 + Flask
├── docker-compose.yml        # 3 client containers, fixed IPs and port maps
├── demo.bat                  # Windows demo launcher (cmd.exe friendly)
├── start_demo.sh             # Linux/macOS demo bootstrap
├── auto_tx.py                # SYSTEM airdrop, then 100 random transactions
├── README.md
└── app/
    ├── p2p_main.py           # Entry point: loads ALL_NODES, starts P2PNode + Flask
    ├── blockchain.py         # P2PNode core: heartbeat / listener / ledger / consensus / trust
    ├── routes.py             # Flask Blueprint: REST API and index page
    └── templates/
        └── index.html        # Single-page Web GUI with node status + trust badge
```

---

## Quick Start

### Requirements

- Docker Desktop (Windows / macOS) or Docker Engine (Linux)
- Python 3 (only required when running `auto_tx.py` to seed data)
- For the full 6-node demo across two hosts: install and log into
  [Tailscale](https://tailscale.com/), then update the Tailscale IPs in
  `app/p2p_main.py` (`ALL_NODES` and `my_ip`) and `app/routes.py` (`NODE_NAME_MAPPING`).

### Single host (3 nodes)

```bash
# 1. (Optional) clean up the previous run's ledger
rm -f ./storage/client{1,2,3}/*.txt

# 2. Start three P2P nodes
docker-compose up -d --build

# 3. Seed test data: SYSTEM airdrop + 100 random transactions
python3 auto_tx.py
```

Open in three browser tabs:

- <http://localhost:8081> — Client 1
- <http://localhost:8082> — Client 2
- <http://localhost:8083> — Client 3

### One-click demo (Windows)

```powershell
demo.bat
```

Menu:

| Option | Action |
| --- | --- |
| 1 | Run `start_demo.sh` via Git Bash or WSL (clean ledger → start containers → inject 100 transactions) |
| 2 | Open six browser tabs at once (localhost x3 + remote host x3) |
| 3 | Wipe `storage/client{1,2,3}/*.txt` |
| 0 | Quit |

### One-click demo (Linux / macOS)

```bash
./start_demo.sh
```

Override the displayed host IP if needed:

```bash
HOST_IP=100.94.194.29 ./start_demo.sh
```

---

## Web GUI

Every node serves the same single-page UI:

- **Network status panel** — six dots showing each node's liveness (online / offline / self),
  a `trusted / frozen` badge, and a `online_count / total` counter. Updates every 1.5 s.
- **Balance & log query** — calls `/api/money/<account>` and `/api/log/<account>`.
- **Transaction form** — submits `POST /api/transaction`.
- **Local ledger validation** — calls `/api/checkChain`, which auto-repairs from the majority on mismatch.
- **Network-wide consensus** — calls `/api/checkAllChains/<target>`; awards 100 coins to `<target>` on success.
- **System log terminal** — drains `/api/poll_logs` every 1.5 s and renders multi-line messages with `<br>` line breaks.

Default test accounts seeded by `auto_tx.py`:
`Darren / Alice / Bob / Charlie / Eve`, each starting with 50,000.

---

## Demo Walkthrough

A typical live-demo storyline:

1. **Start the system** and wait a few seconds. The GUI's network status panel
   should light up: 3 dots green (online) + 3 dots grey (offline, the remote host).
   The trust badge reads **trusted**.
2. **Run `auto_tx.py`** to inject 100 random transactions; the log terminal
   streams `[同步] 收到交易 ...` lines on every node.
3. **Tamper with a ledger**, e.g. on Client 2:
   - `docker exec client2 sh -c 'echo "Hacker, Victim, 999" >> /storage/22.txt'`,
     or `rm /storage/1.txt /storage/2.txt`, etc.
4. From a **healthy** node (Client 1), click **Network-wide Consensus**:
   - Initiator broadcasts `REQ_HASH` to every **live** peer.
   - Hash votes are tallied, ignoring `INVALID` / `EMPTY`.
   - Majority is reached → `BROADCAST_MAJORITY` tells the network the right hash.
   - Tampered Client 2 sees its hash differs, sends `REQ_SYNC` to the provider,
     receives the full ledger, wipes `/storage`, and rewrites every block.
   - Client 2 reports `REPAIR_DONE` → initiator logs **"節點 ... 修復完成"**.
   - Initiator's hash matches the majority → 100-coin SYSTEM reward, then
     `BROADCAST_TRUST` keeps the network's trust badge green.
5. **Force a 3-good / 3-broken scenario** in a 6-node run by corrupting three
   ledgers without updating their hash chain, then run consensus:
   - The first tally is `未達過半 (3/6)`.
   - Because the bad peers are `INVALID` / `EMPTY`, healthy nodes push a rescue
     ledger (or a broken initiator pulls from the largest valid group).
   - The initiator immediately re-checks hashes; once the rescue creates 4/6 or
     better, `BROADCAST_TRUST` unfreezes the network in the same run.
6. **Force a true split-brain scenario** by creating two internally valid ledger
   histories with equal votes. With no clearly broken peer to rescue, consensus
   freezes via `BROADCAST_DISTRUST` until an operator restores or removes the
   ambiguous fork.

---

## REST API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | Web GUI home page |
| GET | `/api/peers` | Live status for all 6 nodes + `network_trusted` / `network_trusted_reason` |
| GET | `/api/money/<account>` | Balance (`null` if local ledger invalid or network frozen) |
| GET | `/api/log/<account>` | Transactions involving the account (empty array if frozen) |
| POST | `/api/transaction` | Body: `{"sender","receiver","amount","tx_id?"}`; optional `tx_id` makes timeout retries idempotent |
| GET | `/api/checkChain` | Validates the local chain; auto-repairs from the majority on mismatch |
| GET | `/api/checkAllChains/<target>` | Runs network-wide consensus; rewards `<target>` with 100 on success |
| GET | `/api/poll_logs` | Drains and returns the node log buffer |
| GET | `/api/stats` | Returns total transaction count |

> `sender = "SYSTEM"` is a privileged account that bypasses balance and trust
> checks (used for the genesis airdrop and consensus rewards).

Sample `/api/peers` response:

```json
{
  "peers": [
    {"node_id":"100.94.194.29-8001","name":"Node 1","ip":"100.94.194.29","port":8001,
     "online":true,"is_self":true,"last_seen_ago":0},
    {"node_id":"100.94.194.29-8002","name":"Node 2","ip":"100.94.194.29","port":8002,
     "online":true,"is_self":false,"last_seen_ago":1.8},
    ...
  ],
  "online_count": 3,
  "total": 6,
  "network_trusted": true,
  "network_trusted_reason": ""
}
```

---

## P2P Message Protocol

All nodes exchange plain-text UDP datagrams on ports 8001-8003.
Every authenticated message carries `network_token = "MY_BLOCKCHAIN_SECRET_2026"`.

### Heartbeat

| Message | Direction | Purpose |
| --- | --- | --- |
| `PING:<node_id>:<token>` | every 2 s, broadcast | Keepalive; receiver replies with `PONG` and updates `peer_last_seen` |
| `PONG:<node_id>:<token>` | reply | Updates the sender's view of who's online |

### Transactions & Ledger Sync

| Message | Direction | Purpose |
| --- | --- | --- |
| `TX2:<sender>:<receiver>:<amount>:<prev_hash>:<after_hash>:<initiator_id>:<token>` | broadcast | Reliable transaction replication. Receiver appends only when its current hash matches `prev_hash`; otherwise it requests `REQ_SYNC` from the initiator |
| `TX:<sender>:<receiver>:<amount>` | broadcast | Legacy transaction format kept for compatibility |
| `REQ_HASH` | 1 → N (live peers only) | Ask peers to report the SHA-256 of their latest block |
| `RESP_HASH:<hash>:<node_id>:<token>` | N → 1 | Hash reply; packets with a wrong `token` are dropped |
| `REQ_SYNC` | point-to-point | Damaged node asks the provider for the full ledger |
| `RESP_SYNC:<json>` | provider → damaged node | Full ledger as JSON; receiver wipes `/storage` and rewrites every file |
| `REPAIR_DONE:<node_id>` | damaged node → initiator | Confirms repair completion |

### Consensus & Trust Broadcast

| Message | Direction | Purpose |
| --- | --- | --- |
| `BROADCAST_MAJORITY:<hash>:<provider_id>:<initiator_id>` | initiator → live peers | Announce the majority hash and where to fetch it from |
| `BROADCAST_DISTRUST:<reason>:<initiator_id>:<token>` | initiator → live peers | Consensus failed (no majority / all invalid) — every receiver freezes itself |
| `BROADCAST_TRUST:<initiator_id>:<token>` | initiator → live peers | Consensus succeeded with initiator in the majority — every receiver unfreezes |

`<reason>` strings never contain `:` (initiator replaces any with the full-width
`：` so the protocol parser stays simple).

---

## Ledger File Format

Every container mounts its own `/storage`:

```
storage/client1/
├── 1.txt
├── 2.txt
├── ...
└── latest_hash.txt
```

Each `<n>.txt` is one block:

```
Sha256 of previous block: <sha256 of the previous file (or "0" for block 1)>
Next block: <n+1>.txt or None
sender, receiver, amount
sender, receiver, amount
... (up to 5 entries)
```

- Block 1's `Sha256 of previous block:` is hard-coded to `0` (genesis marker).
- Once a block reaches 5 transactions, a new file is created. The old block's
  `Next block:` line is updated, and the new block records the previous block's hash.
- `latest_hash.txt` always stores the SHA-256 of the most recent block. This
  catches attacks that only modify the latest block while leaving every earlier
  `prev_hash` intact.

---

## Ledger Integrity Checks

`P2PNode._check_chain_unlocked()` runs four layered checks. If any layer fails
the chain is reported `INVALID`, every consensus / balance / transaction call is
refused, and consensus from a peer can repair it:

1. **Empty-ledger discrimination** —
   - No blocks **and** no `latest_hash.txt` → genesis (legitimate, allows the
     first transaction to be written).
   - No blocks **but** `latest_hash.txt` exists → "all blocks deleted" attack,
     reported as `所有帳本區塊遺失`.
2. **Block-ID contiguity** — actual block IDs must be exactly `[1, 2, …, N]`.
   Deletions, renumbering, or fake gaps surface as
   `區塊編號不連續 (缺漏: [...], 多餘: [...])`.
3. **Genesis check** — block 1's `Sha256 of previous block:` must be `0`.
   Otherwise `創世區塊 1.txt 的 prev_hash 不是 0`.
4. **Prev-hash chain & head check** — for every consecutive pair, recorded
   `prev_hash` must match the actual SHA-256 of the previous file. The last
   file's hash must match `latest_hash.txt`.

| Tampering | Detection layer |
| --- | --- |
| Edit middle / earlier block content | (4) prev-hash chain breaks |
| Edit last block content | (4) `latest_hash.txt` mismatch |
| Delete middle block | (2) contiguity break (gap) |
| Delete oldest blocks (1, 2, …) | (2) actual IDs start above 1 |
| Delete every numbered block | (1) `latest_hash.txt` still present |
| Inject a fake "0.txt" earlier than genesis | (3) prev_hash of new earliest block isn't 0 |
| Delete last blocks but keep `latest_hash.txt` | (4) head hash mismatch |

---

## Heartbeat & Live-Node Consensus

`P2PNode._heartbeat_loop()` runs in its own thread:

- Every `HEARTBEAT_INTERVAL = 2` seconds, send `PING:<node_id>:<token>` to all peers.
- Receiver replies with `PONG:<node_id>:<token>` (also treats the incoming
  `PING` as proof of life, updating its own `peer_last_seen`).
- Anyone whose last `PONG` is older than `HEARTBEAT_TIMEOUT = 5` seconds is
  considered offline.

`get_live_peer_ids()` returns the current online set; `get_peer_status()`
returns the structured rows that drive the GUI.

Consensus uses the **live count**, not the static peer list:

```python
total_expected = len(live_peers) + 1   # +1 = self
if total_expected < MIN_QUORUM_NODES:  # MIN_QUORUM_NODES = 2
    return "存活節點不足 (X/2)"
if max_count > total_expected / 2:
    # majority reached — the chain that wins is whatever ≥ 50%+1 of
    # the live nodes vote for, regardless of how many are offline
```

This means:

- 6 nodes online → majority needs ≥ 4
- 3 nodes online → majority needs ≥ 2
- 1 node online → consensus refuses (quorum floor)

`BROADCAST_MAJORITY` and `BROADCAST_DISTRUST / TRUST` are also targeted at the
live set only — there's no point spamming offline peers with UDP datagrams.

---

## Network Trust State (Freeze / Unfreeze)

Each node holds `self.network_trusted` (default `True`). The state changes
**only** in `_execute_checkAllChains` after a consensus run:

| Outcome | Initiator's local change | What's broadcast |
| --- | --- | --- |
| Live nodes < 2 | unchanged | nothing |
| All votes `INVALID` / `EMPTY` | `False`, reason: `全網均無效帳本` | `BROADCAST_DISTRUST` |
| Majority found, my hash matches | `True`, reason cleared | `BROADCAST_TRUST` |
| Majority found, my hash differs (I'm being repaired) | unchanged this round; next consensus run promotes to `True` | `BROADCAST_MAJORITY` (repair) |
| No majority, but some peers are `INVALID` / `EMPTY` | unchanged while rescue runs; flips to `True` if the rescue follow-up reaches majority | TCP/`REQ_SYNC` repair, then `BROADCAST_TRUST` if follow-up passes |
| No majority and no clearly broken peer to rescue | `False`, reason: `無法達成過半數共識 (X/Y)` | `BROADCAST_DISTRUST` |

Receivers handle the broadcasts unconditionally:

- `BROADCAST_DISTRUST` → set `network_trusted = False`, record reason as
  *"來自 X 的全網共識失敗通知（…）"*.
- `BROADCAST_TRUST` → set `network_trusted = True`, clear reason.

While a node is frozen, `_require_network_trust` blocks:

- `_execute_checkMoney` → returns `None` and logs `查詢餘額已凍結`.
- `_execute_checkLog` → returns `[]` and logs `查詢明細已凍結`.
- `_execute_transaction` (non-SYSTEM) → raises `ValueError(交易已凍結 …)` which
  the route layer turns into HTTP 400.

`SYSTEM` transactions (consensus rewards, genesis airdrop) bypass the trust
check, so the post-consensus 100-coin reward still goes out.

---

## Consensus & Self-Repair

`P2PNode._execute_checkAllChains()` is the heart of the system:

1. **Vote request** — broadcast `REQ_HASH` to every **live** peer (live = last
   `PONG` within 5 s).
2. **Collect votes** — within `SYNC_WAIT_SECONDS = 2`, accept only `RESP_HASH`
   replies that carry the right token and come from a peer in `nodes_contact_book`.
3. **Quorum gate** — if `total_expected < MIN_QUORUM_NODES`, refuse the run
   (state untouched).
4. **Tally** — count with `Counter`, ignoring `INVALID` and `EMPTY`.
5. **Decide** — declare consensus only when `max_count > total_expected / 2`.
6. **Announce** — send `BROADCAST_MAJORITY` so every live node knows the right
   hash and provider.
7. **Self-repair** — any node whose local hash differs from the majority sends
   `REQ_SYNC` to the provider. The provider replies with `RESP_SYNC:<json>`;
   the receiver wipes `/storage` and rewrites every file.
8. **Report back** — the repaired node sends `REPAIR_DONE` to the initiator,
   which logs `節點 ... 修復完成`.
9. **Reward & trust** — if the initiator's own hash matches the majority, it
   credits 100 coins to `<target>`, broadcasts the matching `TX2:` message, and
   sends `BROADCAST_TRUST` to clear any frozen state on peers.
10. **Rescue path** — if no majority but one side is clearly `INVALID` /
    `EMPTY`, healthy peers push the ledger or the broken initiator pulls from
    the largest valid group. The initiator immediately re-votes; if the repair
    creates a majority, it broadcasts `BROADCAST_TRUST` in the same run.
11. **Failure path** — if no majority and no clearly broken peer can be rescued,
    the initiator freezes itself and broadcasts `BROADCAST_DISTRUST`, freezing
    every live peer too.

Defensive design choices:

- **Token check** blocks unauthorized hash injection.
- **Keying votes by `sender_id`** prevents a single malicious peer from stuffing replies.
- **Filtering `INVALID` / `EMPTY`** keeps broken nodes from skewing the majority.
- **Live-peer scoping** — heartbeat-derived live set is the denominator, so
  offline peers don't artificially raise the majority bar.
- **Hard quorum floor** — refuses to run with fewer than 2 live nodes, blocking
  the degenerate "alone, therefore trusted" case.
- **Pre-write validation** — every transaction calls `_check_chain_unlocked()`
  first, so an unhealthy node refuses to write new entries until repair finishes.

---

## FAQ

**Q1. The demo broke after I changed an IP — why?**
Several Tailscale IPs are hard-coded: `ALL_NODES` and `my_ip` in `app/p2p_main.py`,
plus `NODE_NAME_MAPPING` in `app/routes.py`. Update all three together.

**Q2. Why is the `demo.bat` menu in English only?**
`cmd.exe` parses `.bat` files using the system ANSI codepage (cp950 on zh-TW),
which mangles UTF-8 Chinese characters into bogus commands. Plain ASCII is the
safest choice.

**Q3. Why do my freshly-restarted peers show "offline" for the first ~5 seconds?**
Heartbeat needs a full round-trip (PING → PONG) before `peer_last_seen` is
populated. Until that arrives, every peer is treated as offline. Once the first
`PONG` lands the dot turns green; with `INTERVAL=2 s` and `TIMEOUT=5 s` you
should see a stable green within 4-5 seconds of startup.

**Q4. The badge went red after a tampering test — how do I clear it?**
If the tampered ledgers fail local integrity checks (`INVALID` / `EMPTY`), run
**Network-wide Consensus** again. The rescue path repairs the broken nodes and
immediately performs a follow-up vote; once a majority forms,
`BROADCAST_TRUST` flips every peer back to green. If the red badge came from an
equal split between two internally valid histories, restore the intended ledger
or take one fork offline first, because the system cannot safely choose between
two valid 3/3 histories by itself.

**Q5. Can I run with only 3 nodes (one host)?**
Yes — that's the default configuration. With three nodes online the live
denominator is 3 and the majority threshold drops to 2. Stopping any single
node still leaves 2/2 consensus working; stopping two of them trips the quorum
floor and consensus refuses to run.

**Q6. Can I run more than 6 nodes?**
Yes. Add the new entry to `ALL_NODES`, declare additional services in
`docker-compose.yml`, and expose the matching UDP port. Update `NODE_NAME_MAPPING`
in `app/routes.py` so the GUI can label the new nodes.

**Q7. Why do failed transactions tell me the network is frozen?**
A previous consensus run reported no majority (or all invalid), broadcasting
`BROADCAST_DISTRUST` to every live peer. Re-run consensus once the underlying
ledger problem is resolved to receive `BROADCAST_TRUST` and unfreeze the network.

---

## License

Free to use for academic and educational purposes. Please contact the authors
for commercial use.
