import os
import sys
import threading
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))

import blockchain
from blockchain import P2PNode


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, payload, addr):
        self.sent.append((payload, addr))


class ConsensusRescueTest(unittest.TestCase):
    def setUp(self):
        self.original_sync_wait = blockchain.SYNC_WAIT_SECONDS
        blockchain.SYNC_WAIT_SECONDS = 0

    def tearDown(self):
        blockchain.SYNC_WAIT_SECONDS = self.original_sync_wait

    def make_node(self):
        node = P2PNode.__new__(P2PNode)
        node.node_id = "NODE_1"
        node.network_token = "TOKEN"
        node.nodes_contact_book = {
            "NODE_2": ("127.0.0.1", 10002),
            "NODE_3": ("127.0.0.1", 10003),
            "NODE_4": ("127.0.0.1", 10004),
            "NODE_5": ("127.0.0.1", 10005),
            "NODE_6": ("127.0.0.1", 10006),
        }
        node.peers = list(node.nodes_contact_book.values())
        node.sock = FakeSock()
        node.network_trusted = False
        node.network_trusted_reason = "old freeze"
        node.logs = []
        node.rewards = []
        node.tx_id_lock = threading.Lock()
        node.tx_id_results = {}
        node.tx_id_inflight = {}
        node.add_log = node.logs.append
        node.get_live_peer_ids = lambda: set(node.nodes_contact_book.keys())
        node._send_ledger_via_tcp = lambda addr: None
        node._schedule_ledger_repair_fanout = lambda reason="": None
        node._execute_transaction = lambda sender, receiver, amount: node.rewards.append(
            (sender, receiver, amount)
        )
        node._execute_transaction_with_receipt = lambda sender, receiver, amount: (
            node.rewards.append((sender, receiver, amount)) or ("prev-hash", "after-hash")
        )
        return node

    def test_invalid_local_node_can_sync_from_plurality_without_majority(self):
        node = self.make_node()
        votes = {
            "NODE_1": "INVALID",
            "NODE_2": "healthy-hash",
            "NODE_3": "healthy-hash",
            "NODE_4": "healthy-hash",
            "NODE_5": "INVALID",
            "NODE_6": "INVALID",
        }

        ok, msg = node._request_sync_from_majority("INVALID", votes, total_expected=6)

        self.assertTrue(ok)
        self.assertIn("救援", msg)
        self.assertIn((b"REQ_SYNC", ("127.0.0.1", 10002)), node.sock.sent)

    def test_valid_local_node_is_not_overwritten_by_non_majority_split(self):
        node = self.make_node()
        votes = {
            "NODE_1": "healthy-hash",
            "NODE_2": "healthy-hash",
            "NODE_3": "healthy-hash",
            "NODE_4": "other-valid-hash",
            "NODE_5": "other-valid-hash",
            "NODE_6": "other-valid-hash",
        }

        ok, msg = node._request_sync_from_majority("healthy-hash", votes, total_expected=6)

        self.assertFalse(ok)
        self.assertIn("不自動覆寫", msg)
        self.assertEqual([], node.sock.sent)

    def test_rescue_followup_majority_unfreezes_and_rewards_in_same_run(self):
        node = self.make_node()
        votes_after_rescue = {
            "NODE_1": "healthy-hash",
            "NODE_2": "healthy-hash",
            "NODE_3": "healthy-hash",
            "NODE_4": "healthy-hash",
            "NODE_5": "INVALID",
            "NODE_6": "INVALID",
        }
        node._collect_last_hash_votes = lambda: ("healthy-hash", votes_after_rescue, 6)

        msg = node._try_finalize_rescue_consensus("Alice")

        self.assertTrue(node.network_trusted)
        self.assertEqual("", node.network_trusted_reason)
        self.assertIn("已形成過半", msg)
        self.assertEqual([("SYSTEM", "Alice", "100")], node.rewards)
        sent_payloads = [payload for payload, _ in node.sock.sent]
        self.assertTrue(any(payload.startswith(b"BROADCAST_TRUST:") for payload in sent_payloads))
        self.assertTrue(any(payload.startswith(b"TX2:SYSTEM:Alice:100:") for payload in sent_payloads))

    def test_local_check_reports_success_after_auto_repair_recheck(self):
        node = self.make_node()
        node.file_lock = threading.Lock()
        checks = iter([
            (False, "帳本鏈在區塊 2 之前斷裂"),
            (True, "沒問題，帳本鏈和最新區塊Hash值匹配成功"),
        ])
        node._check_chain_unlocked = lambda: next(checks)
        node._repair_from_majority = lambda: (True, "向 NODE_2 發起維修請求 (救援)")

        is_valid, msg = node._execute_checkChain(gui_mode=True, auto_repair=True)

        self.assertTrue(is_valid)
        self.assertIn("修復後複檢", msg)
        self.assertIn("發起全網共識驗證解凍", msg)

    def test_reliable_tx_gap_requests_snapshot_from_initiator(self):
        node = self.make_node()
        node.network_trusted = True
        node._get_last_block_hash = lambda: "hash-before-missing-tx"

        ok = node._handle_reliable_tx([
            "TX2",
            "Alice",
            "Bob",
            "100",
            "hash-after-missing-tx",
            "hash-after-current-tx",
            "NODE_2",
            "TOKEN",
        ])

        self.assertTrue(ok)
        self.assertIn((b"REQ_SYNC", ("127.0.0.1", 10002)), node.sock.sent)
        self.assertEqual([], node.rewards)

    def test_client_tx_id_timeout_retry_is_idempotent(self):
        node = self.make_node()
        calls = []

        def execute_once(sender, receiver, amount):
            calls.append((sender, receiver, amount))
            return "prev-hash", "after-hash"

        node._execute_transaction_with_receipt = execute_once

        first = node._execute_client_transaction("Alice", "Bob", "75", tx_id="demo-tx-1")
        second = node._execute_client_transaction("Alice", "Bob", "75", tx_id="demo-tx-1")

        self.assertEqual(first, second)
        self.assertEqual([("Alice", "Bob", "75")], calls)


if __name__ == "__main__":
    unittest.main()
