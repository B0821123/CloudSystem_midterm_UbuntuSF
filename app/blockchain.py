#================================
# M1421070 戴弘奕；M1429012 吳承翰  
#================================
from email.mime import message
import socket
import threading
import os
import hashlib
import time
import subprocess
import json
from collections import Counter

STORAGE_PATH = "/storage"
HEAD_HASH_FILE = os.path.join(STORAGE_PATH, "latest_hash.txt")
SYNC_WAIT_SECONDS = 2
TX_REPAIR_FANOUT_DELAY_SECONDS = 0.75

# 心跳設定：每 HEARTBEAT_INTERVAL 秒對所有 peer 發 PING；
# 若超過 HEARTBEAT_TIMEOUT 秒沒收到回覆，視為離線。
# interval 比 timeout 小（約 2~3 倍）以避免「燈號抖動」。
HEARTBEAT_INTERVAL = 2
HEARTBEAT_TIMEOUT = 8

# 共識最少參與節點數（含自己）。低於此值直接拒絕全網驗證/修復。
MIN_QUORUM_NODES = 2

# ==========================================
# P2P Node 核心類別
# ==========================================
class P2PNode:
    def __init__(self, ip, port, peers, peers_book=None, my_node_id=None):
        """
        peers       : list[(ip, port)]，給 heartbeat / TX broadcast 直接 sendto 用的「實際路由位址」。
                      同 host 的 peer 建議改用 docker 內部主機名（client1/2/3）以避開 hairpin NAT。
        peers_book  : dict[node_id -> (ip, port)]，給共識邏輯查詢 peer 用的「穩定身份對位址」表。
                      key 必須等於對方在 PING/PONG/RESP_HASH 訊息裡填的 sender_id（也就是對方的 node_id）。
                      若不提供，會 fallback 到舊版「以 ip-port 當 id」的 contact book。
        my_node_id  : 顯式指定本機 node_id。建議跟 peers_book 的 key 同源（例如 NODE_1/NODE_2…）。
                      未提供時 fallback 到 NODE_NAME env 或 ip-port。
        """
        self.ip = ip
        self.port = port
        self.peers = peers
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', self.port))

        # 修改 6：TCP listener，專門處理 RESP_SYNC 這類有可能超出 UDP datagram 安全大小的 payload。
        # TCP 與 UDP 可共用同一個 port 號（不同 protocol stack）。
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind(('0.0.0.0', self.port))
        self.tcp_sock.listen(8)

        self.file_lock = threading.Lock()
        self.expected_hashes = {}
        self.awaiting_hashes = False

        self.log_buffer = []
        self.log_lock = threading.Lock()
        self.tx_id_lock = threading.Lock()
        self.tx_id_results = {}
        self.tx_id_inflight = {}
        self.repair_fanout_lock = threading.Lock()
        self.repair_fanout_timer = None

        # node_id 優先順序：呼叫端顯式指定 > NODE_NAME env > ip-port
        self.node_id = my_node_id or os.environ.get("NODE_NAME") or f"{ip}-{port}"
        self.network_token = "MY_BLOCKCHAIN_SECRET_2026"

        # contact book：key 必須與對方在訊息裡帶的 sender_id 一致，
        # 否則 get_live_peer_ids() 與 nodes_contact_book 對不上 → live_peers 永遠是空集合。
        self.nodes_contact_book = {}
        if peers_book:
            self.nodes_contact_book = dict(peers_book)
        else:
            # 舊版相容：以 ip-port 當 id
            for p_ip, p_port in self.peers:
                p_id = f"{p_ip}-{p_port}"
                self.nodes_contact_book[p_id] = (p_ip, p_port)
        self.pending_initiator = None

        # 心跳狀態：peer_id -> 最後一次收到 PONG 的時間戳
        self.peer_last_seen = {}
        self.peer_lock = threading.Lock()

        # 全網信任狀態：預設為 True，全網共識失敗時凍結；通過時恢復。
        self.network_trusted = True
        self.network_trusted_reason = "尚未驗證"

        # 最近一次「觀測到網路上有共識活動」的時間戳。
        # 收到 REQ_HASH / BROADCAST_MAJORITY 時更新；給 _auto_consensus_loop 當閘門用。
        # 目的：使用者手動竄改本地 .txt 後（沒有按全網驗證）不應該被靜默自我修復——
        #      demo 需要使用者親自按下「全網驗證」按鈕、肉眼看到修復過程。
        # 但「網路上有人剛發起過共識、自己卻沒收到 BROADCAST/TCP 推送」這種掉包情境，
        # 仍然要在下一輪 60 秒自我修復，所以維持時間窗口判斷。
        self.last_consensus_observed_at = 0.0

    def add_log(self, msg):
        print(msg)
        with self.log_lock:
            self.log_buffer.append(msg)

    def start(self):
        print(f"[P2P] Listener {self.ip}:{self.port}")
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        # 修改 6：TCP accept loop（處理大 payload 的 RESP_SYNC）
        threading.Thread(target=self._tcp_accept_loop, daemon=True).start()
        # 修改 3：啟動後若本地是空帳本，主動向多數派同步一次
        threading.Thread(target=self._bootstrap_sync, daemon=True).start()
        # 修改 4：背景定期自我體檢，偵測到本地帳本損壞/空白就自動發起共識修復
        threading.Thread(target=self._auto_consensus_loop, daemon=True).start()

    def _heartbeat_loop(self):
        ping_msg = f"PING:{self.node_id}:{self.network_token}".encode('utf-8')
        while True:
            for peer in self.peers:
                try:
                    self.sock.sendto(ping_msg, peer)
                except Exception as e:
                    print(f"[Heartbeat] 發送 PING 給 {peer} 失敗: {e}")
            time.sleep(HEARTBEAT_INTERVAL)

    # ============================================================
    # 修改 6：TCP 通道（專門承載 RESP_SYNC 這類體積偏大、不能容忍丟封包的 payload）
    # ============================================================
    def _tcp_accept_loop(self):
        while True:
            try:
                conn, addr = self.tcp_sock.accept()
                threading.Thread(
                    target=self._handle_tcp_client,
                    args=(conn, addr),
                    daemon=True,
                ).start()
            except Exception as e:
                print(f"[TCP] accept error: {e}")
                time.sleep(1)

    def _handle_tcp_client(self, conn, addr):
        """接收對方 TCP 推送的 RESP_SYNC 整本帳本，並完成本地修復。"""
        try:
            conn.settimeout(15)
            chunks = []
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                chunks.append(data)
            payload = b"".join(chunks).decode('utf-8', errors='replace')

            if payload.startswith("RESP_SYNC:"):
                json_str = payload[len("RESP_SYNC:"):]
                self._unpack_and_repair_ledger(json_str)
                self.add_log(f"[同步/TCP] 本地帳本已修復完成\n來源: {addr[0]}")

                # 與原本 UDP 版相同：若是被 BROADCAST_MAJORITY 引導過來的修復，
                # 修完要回報給當初發起 checkAllChains 的節點。
                initiator_id = getattr(self, "pending_initiator", None)
                if initiator_id and initiator_id in self.nodes_contact_book:
                    self.sock.sendto(
                        f"REPAIR_DONE:{self.node_id}".encode('utf-8'),
                        self.nodes_contact_book[initiator_id],
                    )
                self.pending_initiator = None
            else:
                self.add_log(f"[TCP] 收到未知 payload (前 16 字元: {payload[:16]!r})")
        except Exception as e:
            print(f"[TCP] handle from {addr} error: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _send_ledger_via_tcp(self, target_addr):
        """主動以 TCP 把整本帳本推送給請求修復的節點。"""
        try:
            ledger_data = self._pack_ledger()
            payload = f"RESP_SYNC:{ledger_data}".encode('utf-8')
            # target_addr 是對方的 UDP 來源 (ip, port)；本程式 TCP/UDP 共用同一 port 號
            with socket.create_connection(target_addr, timeout=10) as s:
                s.sendall(payload)
                # 主動 half-close，告訴對方資料已送完
                s.shutdown(socket.SHUT_WR)
            self.add_log(f"[同步/TCP] 已推送帳本至 {target_addr[0]}:{target_addr[1]}")
        except Exception as e:
            self.add_log(f"[同步/TCP] 推送至 {target_addr} 失敗: {e}")

    def _request_sync_from_node(self, provider_id, reason):
        if provider_id in self.nodes_contact_book:
            self.sock.sendto(b"REQ_SYNC", self.nodes_contact_book[provider_id])
            self.add_log(f"[同步] {reason}\n向 {provider_id} 請求帳本快照")
            return True

        self.add_log(f"[同步] {reason}\n找不到 {provider_id}，改由共識修復")
        threading.Thread(target=self._repair_from_majority, daemon=True).start()
        return False

    def _push_ledger_to_live_peers(self, reason="交易補救"):
        live_peers = self._live_peer_addresses()
        if not live_peers:
            self.add_log(f"[同步/TCP] {reason}: 目前沒有可推送的線上節點")
            return

        self.add_log(f"[同步/TCP] {reason}: 推送帳本快照給 {len(live_peers)} 個線上節點")
        for peer in live_peers:
            threading.Thread(
                target=self._send_ledger_via_tcp,
                args=(peer,),
                daemon=True,
            ).start()

    def _schedule_ledger_repair_fanout(self, reason="交易補救"):
        lock = getattr(self, "repair_fanout_lock", None)
        if lock is None:
            self._push_ledger_to_live_peers(reason)
            return

        with lock:
            timer = getattr(self, "repair_fanout_timer", None)
            if timer:
                timer.cancel()
            timer = threading.Timer(
                TX_REPAIR_FANOUT_DELAY_SECONDS,
                self._push_ledger_to_live_peers,
                args=(reason,),
            )
            timer.daemon = True
            self.repair_fanout_timer = timer
            timer.start()

    # ============================================================
    # 修改 3：啟動後 bootstrap —— 若本地空帳本但有存活鄰居，主動同步一次
    # ============================================================
    def _bootstrap_sync(self):
        # 等心跳跑幾輪，讓 peer_last_seen 有資料可判斷
        time.sleep(HEARTBEAT_INTERVAL * 3 + 1)
        try:
            with self.file_lock:
                files = self._ledger_files_unlocked()
            if files:
                # 本地已有帳本，不必 bootstrap
                return
            if not self.get_live_peer_ids():
                self.add_log("[啟動] 本地空帳本但無存活鄰居\n等待後續自動共識")
                return
            self.add_log("[啟動] 偵測本地空帳本\n主動向多數派發起同步")
            ok, msg = self._repair_from_majority()
            self.add_log(f"[啟動] bootstrap 結果: {msg}")
        except Exception as e:
            print(f"[Bootstrap] error: {e}")

    # ============================================================
    # 修改 4：背景自我體檢 —— 定期偵測本地帳本是否損壞/落後並自動修復
    # ============================================================
    def _auto_consensus_loop(self):
        # 開機後緩衝，避免和 bootstrap 撞在一起
        time.sleep(30)
        # 「最近這段時間內，網路上有人剛跑過共識」的時間窗（秒）。
        # 設成 ≥ 兩個 loop 週期（60s）較保險，這樣 worst case 兩輪內都會被當成「最近觀測到」。
        CONSENSUS_WINDOW_SECONDS = 150
        while True:
            try:
                with self.file_lock:
                    files = self._ledger_files_unlocked()
                    # 注意 initialize_missing_head=False：別讓這條 loop 偷偷補 latest_hash.txt，
                    # 否則「全新節點」與「帳本曾被破壞」的判斷會被搞混。
                    is_valid, _ = self._check_chain_unlocked(initialize_missing_head=False)

                # 只在「鏈無效」或「本地空但有鄰居」時才動手；正常運作的節點不要白忙
                needs_repair = (not is_valid) or (not files)

                # 【閘門】只在「最近 CONSENSUS_WINDOW_SECONDS 內收到過 REQ_HASH / BROADCAST_MAJORITY」
                # 才執行修復。也就是說：
                #   - 使用者剛按過全網驗證 → 大家會收到 REQ_HASH → 之後 60 秒內這條 loop 會替「沒收到
                #     BROADCAST_MAJORITY / 沒收到 TCP 推送」的壞節點補上一輪修復（UDP 掉包補救）。
                #   - 使用者只是手動竄改某個 client 的 .txt 但沒按驗證 → 沒有人發 REQ_HASH，
                #     last_consensus_observed_at 還停在很久以前 → 這條 loop 不動，demo 時要使用者
                #     親自按下「全網驗證」才會看到修復過程。
                recently_observed = (
                    time.time() - self.last_consensus_observed_at
                ) < CONSENSUS_WINDOW_SECONDS

                if needs_repair and self.get_live_peer_ids() and recently_observed:
                    ok, msg = self._repair_from_majority()
                    self.add_log(f"[自動共識] 觸發修復: {msg}")
            except Exception as e:
                print(f"[Auto-Consensus] error: {e}")
            time.sleep(60)

    def get_live_peer_ids(self):
        """回傳目前還在線（最後 PONG 在超時內）的 peer node_id 集合。"""
        now = time.time()
        with self.peer_lock:
            return {
                pid for pid, ts in self.peer_last_seen.items()
                if now - ts <= HEARTBEAT_TIMEOUT
            }

    def _live_peer_addresses(self):
        return [
            self.nodes_contact_book[pid]
            for pid in self.get_live_peer_ids()
            if pid in self.nodes_contact_book
        ]

    def get_peer_status(self):
        """提供前端：每個已知 peer 的線上狀態。"""
        now = time.time()
        result = []
        with self.peer_lock:
            for pid, (p_ip, p_port) in self.nodes_contact_book.items():
                last = self.peer_last_seen.get(pid)
                online = last is not None and (now - last) <= HEARTBEAT_TIMEOUT
                result.append({
                    "node_id": pid,
                    "ip": p_ip,
                    "port": p_port,
                    "online": online,
                    "last_seen_ago": None if last is None else round(now - last, 1),
                })
        return result

    def _listen(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
                message = data.decode('utf-8')
                
                if message.startswith("BROADCAST_DISTRUST:"):
                    parts = message.split(":")
                    # 格式：BROADCAST_DISTRUST:{reason}:{initiator_id}:{token}
                    if len(parts) == 4 and parts[3] == self.network_token:
                        reason = parts[1]
                        initiator_id = parts[2]
                        self.network_trusted = False
                        self.network_trusted_reason = f"來自 {initiator_id} 的全網共識失敗通知（{reason}）"
                        self.add_log(f"[共識] 收到 {initiator_id} 凍結通知\n原因: {reason}")
                    continue

                if message.startswith("BROADCAST_TRUST:"):
                    parts = message.split(":")
                    # 格式：BROADCAST_TRUST:{initiator_id}:{token}
                    if len(parts) == 3 and parts[2] == self.network_token:
                        initiator_id = parts[1]
                        was_frozen = not self.network_trusted
                        self.network_trusted = True
                        self.network_trusted_reason = ""
                        if was_frozen:
                            self.add_log(f"[共識] 收到 {initiator_id} 解凍通知\n本機已恢復信任")
                    continue

                if message.startswith("PING:"):
                    parts = message.split(":")
                    if len(parts) == 3 and parts[2] == self.network_token:
                        # 注意：不要在這裡更新 peer_last_seen，
                        # 收到 PING 只代表單向可達，不代表我能回得去。
                        pong = f"PONG:{self.node_id}:{self.network_token}"
                        self.sock.sendto(pong.encode('utf-8'), addr)
                    continue

                if message.startswith("PONG:"):
                    parts = message.split(":")
                    if len(parts) == 3 and parts[2] == self.network_token:
                        sender_id = parts[1]
                        with self.peer_lock:
                            self.peer_last_seen[sender_id] = time.time()
                    continue        

                if message.startswith("TX:"):
                    parts = message.split(":")
                    if len(parts) == 4:
                        # 修改 5：原本 ValueError 會被外層 except 靜默吃掉，demo 時看不到失敗原因，
                        # 也錯失了「我落後了」的訊號。改成顯式攔截 + 自動排程一次共識修復。
                        try:
                            self._execute_transaction(parts[1], parts[2], parts[3])
                            self.add_log(f"[同步] 收到交易\n{parts[1]} -> {parts[2]} ({parts[3]})")
                        except ValueError as e:
                            self.add_log(
                                f"[同步] 拒絕 TX {parts[1]}->{parts[2]}({parts[3]})\n原因: {e}"
                            )
                            # TX 被拒往往意味本機帳本已落後 → 背景觸發一次同步
                            threading.Thread(
                                target=self._repair_from_majority, daemon=True
                            ).start()

                elif message.startswith("TX2:"):
                    self._handle_reliable_tx(message.split(":"))

                elif message.startswith("REQ_HASH"):
                    # 收到 REQ_HASH 代表「網路上有人正在跑共識」——
                    # 這是 _auto_consensus_loop 判斷「能不能自我修復」的核心訊號。
                    self.last_consensus_observed_at = time.time()
                    self.add_log(f"[共識] 回應 Hash 請求 ({addr[0]})")
                    # 組合格式：RESP_HASH : [Hash] : [我的ID] : [安全Token]
                    response = f"RESP_HASH:{self._get_last_block_hash()}:{self.node_id}:{self.network_token}"
                    self.sock.sendto(response.encode('utf-8'), addr)
                    
                elif message.startswith("RESP_HASH:"):
                    if self.awaiting_hashes:
                        parts = message.split(":")
                        print(f"DEBUG: 收到原始回覆 -> {message}")

                        # 檢查格式是否完整 (RESP_HASH + Hash + ID + Token = 4 部分)
                        if len(parts) == 4:
                            h_val = parts[1]
                            sender_id = parts[2]
                            token = parts[3]

                            # 【資安門神】
                            # 1. 暗號不對 -> 滾 (防止外部封包)
                            # 2. 發送者不在通訊錄裡 -> 滾 (防止未授權節點)
                            if token == self.network_token:
                                # 使用 sender_id 作為 Key，防止灌票
                                self.expected_hashes[sender_id] = h_val

                elif message.startswith("BROADCAST_MAJORITY:"):
                    # 同上：收到 BROADCAST_MAJORITY 也代表「網路上剛剛跑過共識」。
                    self.last_consensus_observed_at = time.time()
                    parts = message.split(":")
                    if len(parts) >= 3:
                        majority_hash = parts[1]
                        provider_id = parts[2]
                        initiator_id = parts[3] if len(parts) >= 4 else None  # 新增
                        my_hash = self._get_last_block_hash()
                        # 我才是提供者 -> 無需修復
                        if provider_id == self.node_id:
                            pass
                        elif my_hash != majority_hash:
                            if provider_id in self.nodes_contact_book:
                                provider_addr = self.nodes_contact_book[provider_id]
                                self.add_log(f"[共識] 本地與多數派不符\n向 {provider_id} 請求修復")
                                self.pending_initiator = initiator_id  # 新增：記住誰發起的，修完要回報
                                self.sock.sendto(b"REQ_SYNC", provider_addr)
                            else:
                                self.add_log(f"[共識] 找不到提供者 {provider_id} 的位址")

                elif message.startswith("REQ_SYNC"):
                    last_hash = self._get_last_block_hash()
                    if last_hash in ["INVALID", "EMPTY"]:
                        self.add_log(f"[同步] 拒絕 {addr[0]} 的同步請求\n本地狀態: {last_hash}")
                        continue
                    self.add_log(f"[同步] 回應 {addr[0]} 的修復請求 (TCP 推送)")
                    # 修改 6：以 TCP 推送整本帳本，避免 UDP datagram 大小限制 / 分片在跨機 NAT 下被丟。
                    # addr[1] 是對方的 UDP 監聽 port，本程式 TCP/UDP 共用同一 port 號，因此可直接連回。
                    threading.Thread(
                        target=self._send_ledger_via_tcp,
                        args=(addr,),
                        daemon=True,
                    ).start()

                elif message.startswith("RESP_SYNC:"):
                    json_str = message[len("RESP_SYNC:"):]
                    self._unpack_and_repair_ledger(json_str)
                    self.add_log("[同步] 本地帳本已修復完成")
                    # 新增：把修復完成的事實回報給當初發起 checkAllChains 的節點
                    initiator_id = getattr(self, "pending_initiator", None)
                    if initiator_id and initiator_id in self.nodes_contact_book:
                        self.sock.sendto(
                            f"REPAIR_DONE:{self.node_id}".encode('utf-8'),
                            self.nodes_contact_book[initiator_id]
                        )
                    self.pending_initiator = None

                elif message.startswith("REPAIR_DONE:"):
                    repaired_id = message.split(":", 1)[1]
                    self.add_log(f"[共識] 節點 {repaired_id} 修復完成")

            except Exception as e:
                print(f"[Error] 監聽發生錯誤: {e}")

    def _get_total_tx_count(self):
        with self.file_lock:
            files = self._ledger_files_unlocked()
            if not files: return 0
            
            # 公式實作：(總檔案數 - 1) * 5
            total_from_full_blocks = (len(files) - 1) * 5
            
            # 讀取最後一個檔案看裡面有幾筆交易 (算有逗號的行數)
            with open(os.path.join(STORAGE_PATH, files[-1]), "r") as f:
                last_block_content = f.readlines()
                tx_in_last_block = sum(1 for line in last_block_content if "," in line)
                
            return total_from_full_blocks + tx_in_last_block

# ==========================================
# 帳本與共識邏輯 
# ==========================================
    def _ledger_files_unlocked(self):
        return sorted(
            [
                f for f in os.listdir(STORAGE_PATH)
                if f.endswith(".txt") and f.split('.')[0].isdigit()
            ],
            key=lambda x: int(x.split('.')[0])
        )

    def _get_file_hash(self, file_path):
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def _write_head_hash_unlocked(self, last_hash=None):
        files = self._ledger_files_unlocked()
        if not files:
            return

        if last_hash is None:
            last_hash = self._get_file_hash(os.path.join(STORAGE_PATH, files[-1]))

        with open(HEAD_HASH_FILE, "w") as f:
            f.write(last_hash + "\n")

    def _check_chain_unlocked(self, initialize_missing_head=True):
        files = self._ledger_files_unlocked()
        if not files:
            # 區別「全新節點 (genesis)」與「區塊被刪除」：
            #   - 連 latest_hash.txt 都沒有  → 全新節點，視為合法（讓首筆交易能寫入）
            #   - latest_hash.txt 仍存在     → 代表曾經有帳本後被破壞，視為異常
            if os.path.exists(HEAD_HASH_FILE):
                return False, "所有帳本區塊遺失（latest_hash.txt 仍存在）"
            return True, "全新節點，無帳本區塊"

        # 完整性檢查：區塊編號必須是 1..N 連續、無跳號、無遺失前段
        actual_ids = [int(f.split('.')[0]) for f in files]
        expected_ids = list(range(1, len(files) + 1))
        if actual_ids != expected_ids:
            missing = sorted(set(expected_ids) - set(actual_ids))
            extra = sorted(set(actual_ids) - set(expected_ids))
            detail = []
            if missing:
                detail.append(f"缺漏: {missing[:5]}{'...' if len(missing) > 5 else ''}")
            if extra:
                detail.append(f"多餘: {extra[:5]}{'...' if len(extra) > 5 else ''}")
            return False, f"區塊編號不連續 ({', '.join(detail)})"

        # 創世檢查：第一個區塊的 prev_hash 必須是 "0"
        first_path = os.path.join(STORAGE_PATH, files[0])
        with open(first_path, "r") as f:
            first_prev = f.readline().strip().replace("Sha256 of previous block: ", "")
        if first_prev != "0":
            return False, "創世區塊 1.txt 的 prev_hash 不是 0"

        for i in range(1, len(files)):
            prev_path = os.path.join(STORAGE_PATH, files[i - 1])
            curr_path = os.path.join(STORAGE_PATH, files[i])
            actual_prev_hash = self._get_file_hash(prev_path)
            with open(curr_path, "r") as f:
                recorded_hash = f.readline().strip().replace("Sha256 of previous block: ", "")

            if actual_prev_hash != recorded_hash:
                block_id = files[i].split('.')[0]
                return False, f"帳本鏈在區塊 {block_id} 之前斷裂"

        last_file = files[-1]
        actual_last_hash = self._get_file_hash(os.path.join(STORAGE_PATH, last_file))
        if os.path.exists(HEAD_HASH_FILE):
            with open(HEAD_HASH_FILE, "r") as f:
                expected_last_hash = f.read().strip()

            if actual_last_hash != expected_last_hash:
                block_id = last_file.split('.')[0]
                return False, f"{block_id}被篡改 ."

        elif initialize_missing_head:
            self._write_head_hash_unlocked(actual_last_hash)
            return False, "latest_hash.txt 檔案缺失；已從目前帳本初始化"
        else:
            return False, "latest_hash.txt 檔案缺失"

        return True, "沒問題，帳本鏈和最新區塊Hash值匹配成功"

    def _collect_last_hash_votes(self):
        self.expected_hashes.clear()
        self.awaiting_hashes = True

        # 只向「目前存活」的 peer 索取 hash；通訊錄中的離線節點直接略過。
        live_peers = self._live_peer_addresses()
        for peer in live_peers:
            self.sock.sendto(b"REQ_HASH", peer)

        my_hash = self._get_last_block_hash()
        time.sleep(SYNC_WAIT_SECONDS)
        self.awaiting_hashes = False

        all_votes = self.expected_hashes.copy()
        all_votes[self.node_id] = my_hash
        # 「過半」改用實際存活的節點數（含自己）作為分母
        total_expected = len(live_peers) + 1
        return my_hash, all_votes, total_expected

    def _majority_hash(self, all_votes):
        valid_hashes = Counter(h for h in all_votes.values() if h not in ["INVALID", "EMPTY"])
        if not valid_hashes:
            return None, 0
        return valid_hashes.most_common(1)[0]

    def _request_sync_from_majority(self, my_hash, all_votes, total_expected):
        if total_expected < MIN_QUORUM_NODES:
            return False, f"存活節點不足（{total_expected}/{MIN_QUORUM_NODES}），無法達成共識修復。"

        plurality_hash, max_count = self._majority_hash(all_votes)
        if not plurality_hash:
            return False, "全網均無有效帳本可作為修復來源。"

        is_majority = max_count > total_expected / 2
        am_broken = my_hash in ("INVALID", "EMPTY")

        # =============================================================
        # 救援邏輯：解決「6 個節點中 3 個被竄改 → max_count=3 不過半 → 死鎖」
        #
        # 過去這裡無條件要求過半多數，造成 3/6 壞掉時連自己也救不了自己——
        # 全網驗證 DISTRUST 後系統永遠凍結。
        #
        # 但「我自己已經 INVALID/EMPTY」這件事本身就是強訊號：
        # 本機 4 層完整性檢查已經證明我目前的資料是錯的。在這個前提下，
        # 任何 valid peer 的資料都嚴格優於我目前的，所以即使沒過半也應該拉。
        # 這個放寬只覆蓋「明顯壞掉」的情況，不會放寬「兩派 valid 誰是真的」的判斷。
        # =============================================================
        if not is_majority and not am_broken:
            # 我自己看起來正常 + 網路沒過半 → 不敢擅自改自己，可能誤判
            return False, f"未達過半 ({max_count}/{total_expected})，且本機帳本目前 valid，不自動覆寫。"

        if my_hash == plurality_hash:
            return True, "本地帳本已與最大有效群組一致。"

        provider_id = [node_id for node_id, h in all_votes.items() if h == plurality_hash][0]
        if provider_id not in self.nodes_contact_book:
            return False, f"找不到 provider {provider_id} 的位址。"

        self.sock.sendto(b"REQ_SYNC", self.nodes_contact_book[provider_id])
        mode = "多數派" if is_majority else "救援（本機已壞，套用最大有效群組）"
        return True, f"向 {provider_id} 發起維修請求 ({mode})"

    def _complete_successful_consensus(
        self,
        target,
        my_hash,
        all_votes,
        total_expected,
        majority_hash,
        max_count,
        live_peers,
    ):
        output_msg = ""

        # 找出所有「實名制」回報但與多數派不一致的節點。
        # 同時保留兩份：display（給使用者看）與 raw node_id（直接推送 TCP 用）。
        tampered_display = []
        tampered_node_ids = []
        for nid, h in all_votes.items():
            if h != majority_hash:
                tag = "INVALID" if h == "INVALID" else ("EMPTY" if h == "EMPTY" else h[:12] + "...")
                tampered_display.append(f"{nid}(Hash={tag})")
                tampered_node_ids.append(nid)

        # provider_id 只有在有節點需要修復時才會用到；
        # 自己在多數派時直接把自己當 provider（符合「我發起的、我也對、就以我為準」）。
        if my_hash == majority_hash:
            provider_id = self.node_id
        else:
            provider_id = next(node_id for node_id, h in all_votes.items() if h == majority_hash)

        if tampered_display:
            detail = "、".join(tampered_display)
            output_msg += f"\n異常節點: {detail}"
            self.add_log(f"[共識] 異常節點: {detail}")

            if my_hash == majority_hash:
                # 【快速路徑】我自己就是 provider，手上就有正確資料。
                # 直接平行 TCP 推送整本帳本給每個 tampered peer，避開兩段 UDP 掉包風險。
                push_count = 0
                for nid in tampered_node_ids:
                    if nid in self.nodes_contact_book:
                        target_addr = self.nodes_contact_book[nid]
                        self.add_log(f"[共識] 直接推送帳本給 {nid}")
                        threading.Thread(
                            target=self._send_ledger_via_tcp,
                            args=(target_addr,),
                            daemon=True,
                        ).start()
                        push_count += 1
                    else:
                        self.add_log(f"[共識] 找不到 {nid} 的位址，跳過")
                output_msg += f"\n已直接推送帳本給 {push_count} 個節點"
            else:
                # 【慢速路徑】我自己也壞了，沒有正確資料可推；
                # 改走原本的廣播：通知所有 peer 去 provider 那邊要、自己也發 REQ_SYNC。
                broadcast_msg = f"BROADCAST_MAJORITY:{majority_hash}:{provider_id}:{self.node_id}"
                for peer in live_peers:
                    self.sock.sendto(broadcast_msg.encode('utf-8'), peer)
                self.add_log(f"[共識] 已廣播修復通知\n提供者: {provider_id}")

                output_msg += f"\n本機與多數派不符\n正在向 {provider_id} 修復"
                self.add_log(f"[同步] 本機帳本異常\n向 {provider_id} 請求修復")
                if provider_id in self.nodes_contact_book:
                    self.sock.sendto(b"REQ_SYNC", self.nodes_contact_book[provider_id])

            # 等待 TCP 推送 / REQ_SYNC-RESP_SYNC 完成，再廣播獎勵交易，
            # 否則 TX 會在還沒修好的節點上因本地帳本無效而被拒絕。
            time.sleep(SYNC_WAIT_SECONDS)
        else:
            self.add_log("[共識] 全網一致\n無需修復")

        if my_hash == majority_hash:
            # 多數派一致 + 我也在多數派 → 解凍 + 廣播解凍給全網
            self.network_trusted = True
            self.network_trusted_reason = ""
            self._broadcast_trust(live_peers)
            output_msg += f"\n共識通過 ({max_count}/{total_expected})\n獎勵: 100 -> {target}"
            prev_hash, after_hash = self._execute_transaction_with_receipt("SYSTEM", target, "100")
            self._broadcast_transaction("SYSTEM", target, "100", prev_hash, after_hash)
        else:
            output_msg += f"\n本機已向 {provider_id} 修復\n本輪不發放獎勵"

        return output_msg

    def _try_finalize_rescue_consensus(self, target):
        """救援後立刻複驗一次；若已形成過半，直接完成解凍流程。"""
        my_hash, all_votes, total_expected = self._collect_last_hash_votes()
        majority_hash, max_count = self._majority_hash(all_votes)

        if total_expected < MIN_QUORUM_NODES:
            return f"\n救援後複驗：存活節點不足 ({total_expected}/{MIN_QUORUM_NODES})，暫不解凍"

        if not majority_hash:
            return "\n救援後複驗：仍沒有有效帳本，暫不解凍"

        if max_count <= total_expected / 2:
            return f"\n救援後複驗：仍未達過半 ({max_count}/{total_expected})，暫不解凍"

        live_peers = self._live_peer_addresses()
        output_msg = f"\n救援後複驗：已形成過半 ({max_count}/{total_expected})"
        output_msg += self._complete_successful_consensus(
            target,
            my_hash,
            all_votes,
            total_expected,
            majority_hash,
            max_count,
            live_peers,
        )
        return output_msg

    def _repair_from_majority(self):
        my_hash, all_votes, total_expected = self._collect_last_hash_votes()
        return self._request_sync_from_majority(my_hash, all_votes, total_expected)

    def _broadcast_distrust(self, reason, live_peers):
        """共識失敗時，通知所有存活節點同步凍結。"""
        # 確保 reason 不含 ':' 干擾協定切分
        safe_reason = reason.replace(":", "：")
        msg = f"BROADCAST_DISTRUST:{safe_reason}:{self.node_id}:{self.network_token}"
        for peer in live_peers:
            try:
                self.sock.sendto(msg.encode('utf-8'), peer)
            except Exception as e:
                print(f"[Broadcast Distrust] 發送給 {peer} 失敗: {e}")
        self.add_log(f"[共識] 已廣播凍結通知\n原因: {reason}")

    def _broadcast_trust(self, live_peers):
        """共識成功且本機在多數派時，通知所有存活節點同步解凍。"""
        msg = f"BROADCAST_TRUST:{self.node_id}:{self.network_token}"
        for peer in live_peers:
            try:
                self.sock.sendto(msg.encode('utf-8'), peer)
            except Exception as e:
                print(f"[Broadcast Trust] 發送給 {peer} 失敗: {e}")
        self.add_log("[共識] 已廣播解凍通知")

    def _require_network_trust(self, action_name):
        """全網共識失敗時，凍結所有金流相關操作。回傳 (ok, msg)。"""
        if not self.network_trusted:
            msg = (
                f"{action_name}已凍結\n"
                f"原因: {self.network_trusted_reason}\n"
                f"請重新發起全網共識驗證"
            )
            self.add_log(msg)
            return False, msg
        return True, None

    def _execute_checkMoney(self, target, gui_mode=False):
        ok, _ = self._require_network_trust("查詢餘額")
        if not ok:
            return None

        is_valid = self._execute_checkChain()
        if not is_valid:
            # 如果帳本損毀，直接報錯或回傳 None，不進行後續計算
            self.add_log("[安全] 拒絕查詢餘額\n本地帳本受損，請先進行共識修復")
            return None # 或是回傳 0，視你的前端邏輯而定
        
        balance = 0
        with self.file_lock:
            files = self._ledger_files_unlocked()
            for file in files:
                with open(f"{STORAGE_PATH}/{file}", "r") as f:
                    for line in f:
                        if "," in line:
                            parts = [p.strip() for p in line.split(",")]
                            if len(parts) == 3:
                                if parts[0] == target: balance -= int(parts[2])
                                if parts[1] == target: balance += int(parts[2])
        return balance

    def _execute_checkLog(self, target, gui_mode=False):
        ok, _ = self._require_network_trust("查詢明細")
        if not ok:
            return [] if gui_mode else None

        logs = []
        with self.file_lock:
            files = self._ledger_files_unlocked()
            for file in files:
                with open(f"{STORAGE_PATH}/{file}", "r") as f:
                    for line in f:
                        if "," in line and target in line: logs.append(line.strip())
        if gui_mode: return logs

    def _execute_checkChain(self, gui_mode=False, print_result=False, auto_repair=False):
        with self.file_lock:
            is_valid, msg = self._check_chain_unlocked()

        if not is_valid and auto_repair:
            repaired, repair_msg = self._repair_from_majority()
            msg = f"{msg} 自動修復: {repair_msg}"
            if repaired:
                self.add_log(f"[AUTO_REPAIR] {msg}")
                time.sleep(SYNC_WAIT_SECONDS)
                with self.file_lock:
                    repaired_valid, repaired_msg = self._check_chain_unlocked()
                if repaired_valid:
                    is_valid = True
                    msg = (
                        f"{msg}；修復後複檢: {repaired_msg}"
                        "。若全網仍凍結，請發起全網共識驗證解凍。"
                    )
                else:
                    msg = f"{msg}；修復後複檢仍失敗: {repaired_msg}"

        return (is_valid, msg) if gui_mode else is_valid

    def _get_last_block_hash(self):
        res = self._execute_checkChain()
        is_valid = res[0] if type(res) == tuple else res
        if not is_valid: return "INVALID"
        with self.file_lock:
            files = self._ledger_files_unlocked()
            if not files: return "EMPTY"
            return self._get_file_hash(os.path.join(STORAGE_PATH, files[-1]))

    def _pack_ledger(self):
        ledger_dict = {}
        with self.file_lock:
            self._write_head_hash_unlocked()
            for file in [f for f in os.listdir(STORAGE_PATH) if f.endswith(".txt")]:
                with open(f"{STORAGE_PATH}/{file}", "r") as f: ledger_dict[file] = f.read()
        return json.dumps(ledger_dict)

    def _unpack_and_repair_ledger(self, json_str):
        try:
            ledger_dict = json.loads(json_str)
            with self.file_lock:
                for f in os.listdir(STORAGE_PATH):
                    if f.endswith(".txt"): os.remove(os.path.join(STORAGE_PATH, f))
                for filename, content in ledger_dict.items():
                    with open(os.path.join(STORAGE_PATH, filename), "w") as f: f.write(content)
                self._write_head_hash_unlocked()
                self.add_log("[同步] 本地帳本已被覆寫更新")
        except Exception as e: print(f"[Error] 解析失敗: {e}")

    def _execute_checkAllChains(self, target, gui_mode=False):
        # 1. 初始化收集箱
        self.expected_hashes.clear()
        self.awaiting_hashes = True

        # 2. 只發送請求給「目前存活」的節點
        live_ids = self.get_live_peer_ids()
        live_peers = [
            self.nodes_contact_book[pid] for pid in live_ids
            if pid in self.nodes_contact_book
        ]
        for peer in live_peers:
            self.sock.sendto(b"REQ_HASH", peer)

        # 3. 整合選票 (包含自己的一票)
        my_hash = self._get_last_block_hash()
        time.sleep(SYNC_WAIT_SECONDS)
        self.awaiting_hashes = False

        all_votes = self.expected_hashes.copy()
        all_votes[self.node_id] = my_hash
        # 「過半」分母 = 實際存活節點 + 自己
        total_expected = len(live_peers) + 1
        
        output_msg = (
            f"[共識結果]\n"
            f"參與節點: {total_expected}\n"
            f"收到回覆: {len(all_votes)}"
        )

        # 硬門檻：存活節點（含自己）少於 MIN_QUORUM_NODES 一律拒絕共識
        if total_expected < MIN_QUORUM_NODES:
            output_msg += (
                f"\n存活節點不足 ({total_expected}/{MIN_QUORUM_NODES})\n"
                f"請等待其他節點上線"
            )
            return output_msg if gui_mode else None

        # 4. 統計出現次數最多的 Hash並排除掉無效的 Hash (例如 INVALID 或 EMPTY)
        valid_hashes = Counter(h for h in all_votes.values() if h not in ["INVALID", "EMPTY"])

        if not valid_hashes:
            self.network_trusted = False
            self.network_trusted_reason = "全網均無效帳本"
            self._broadcast_distrust("全網均無效帳本", live_peers)
            return (output_msg + "\n全網均無效帳本") if gui_mode else None

        majority_hash, max_count = valid_hashes.most_common(1)[0]

        # 5. 判斷是否過半數
        if max_count > total_expected / 2:
            output_msg += self._complete_successful_consensus(
                target,
                my_hash,
                all_votes,
                total_expected,
                majority_hash,
                max_count,
                live_peers,
            )
        else:
            # =================================================================
            # 未達過半 —— 救援邏輯
            #
            # 過去這裡無條件 DISTRUST，造成「6 個節點裡 3 個被竄改」就永遠凍結
            # （第一次驗證 DISTRUST，再驗一次還是 DISTRUST，被竄改的節點本機檢查
            #  也救不了自己，因為救援也要求過半）。
            #
            # 新邏輯：先看能不能局部救援，再決定要不要 DISTRUST：
            #   - 若我在 plurality 且有明顯壞掉 (INVALID/EMPTY) 的 peer
            #     → 直接 TCP 推送給他們（用我手上的 valid 資料）
            #   - 若我自己壞了 + 網路有 plurality（雖然沒過半）
            #     → 向 plurality 的 provider 發 REQ_SYNC，讓自己被救
            #   - 兩種情況都成立時兩種一起做
            # 任一救援成功，下一次驗證就能形成過半 → 自動解凍。
            # 只有「純分裂、沒有 INVALID 節點可救」才真的 DISTRUST。
            # =================================================================
            output_msg += f"\n未達過半 ({max_count}/{total_expected})"

            broken_peer_ids = [
                nid for nid, h in all_votes.items()
                if h in ("INVALID", "EMPTY") and nid != self.node_id
            ]
            am_broken = my_hash in ("INVALID", "EMPTY")
            i_have_valid_data = (my_hash == majority_hash) and not am_broken

            rescue_attempted = False

            # 救援 1：我有正確資料 + 有明顯壞掉的 peer → 直接 TCP 推送
            if i_have_valid_data and broken_peer_ids:
                push_count = 0
                for nid in broken_peer_ids:
                    if nid in self.nodes_contact_book:
                        target_addr = self.nodes_contact_book[nid]
                        self.add_log(f"[共識/救援] 推送帳本給壞掉的 {nid}")
                        threading.Thread(
                            target=self._send_ledger_via_tcp,
                            args=(target_addr,),
                            daemon=True,
                        ).start()
                        push_count += 1
                if push_count > 0:
                    output_msg += f"\n本機在最大有效群組\n已救援 {push_count} 個壞節點"
                    rescue_attempted = True

            # 救援 2：我自己壞了 → 拉 plurality 的資料救自己
            if am_broken:
                provider_id_for_self = next(
                    (nid for nid, h in all_votes.items() if h == majority_hash),
                    None,
                )
                if provider_id_for_self and provider_id_for_self in self.nodes_contact_book:
                    self.sock.sendto(
                        b"REQ_SYNC",
                        self.nodes_contact_book[provider_id_for_self],
                    )
                    self.add_log(f"[共識/救援] 本機已壞\n向 {provider_id_for_self} 拉取救援資料")
                    output_msg += f"\n本機已壞，正在向 {provider_id_for_self} 救援"
                    rescue_attempted = True

            if rescue_attempted:
                # 留一段時間等 TCP 推送 / REQ_SYNC-RESP_SYNC 完成
                time.sleep(SYNC_WAIT_SECONDS)
                output_msg += "\n救援已派出，正在複驗是否可解凍"
                output_msg += self._try_finalize_rescue_consensus(target)
            else:
                # 真的沒救：純分裂（沒有 INVALID 節點可救）或全網皆無 valid hash
                self.network_trusted = False
                reason = f"無法達成過半數共識 ({max_count}/{total_expected})"
                self.network_trusted_reason = reason
                self._broadcast_distrust(reason, live_peers)

        if gui_mode: return output_msg

    def _execute_transaction_with_receipt(self, sender, receiver, amount):
        prev_hash = self._get_last_block_hash()
        self._execute_transaction(sender, receiver, amount)
        after_hash = self._get_last_block_hash()
        return prev_hash, after_hash

    def _broadcast_transaction(self, sender, receiver, amount, prev_hash, after_hash):
        msg = (
            f"TX2:{sender}:{receiver}:{amount}:"
            f"{prev_hash}:{after_hash}:{self.node_id}:{self.network_token}"
        )
        payload = msg.encode('utf-8')
        for peer in self.peers:
            try:
                self.sock.sendto(payload, peer)
            except Exception as e:
                self.add_log(f"[同步] TX2 發送至 {peer} 失敗，等待 TCP 補救: {e}")

        # UDP 是快路徑；debounced TCP ledger snapshot 是保險。
        # 這會補上「最後一筆 TX2 剛好掉包」且後面沒有下一筆可觸發缺口偵測的情況。
        self._schedule_ledger_repair_fanout("交易廣播補救")

    def _handle_reliable_tx(self, parts):
        if len(parts) != 8:
            self.add_log("[同步] 收到格式錯誤的 TX2，已忽略")
            return False

        _, sender, receiver, amount, prev_hash, after_hash, initiator_id, token = parts
        if token != self.network_token:
            self.add_log("[同步] 收到 token 不符的 TX2，已忽略")
            return False

        my_hash = self._get_last_block_hash()
        if my_hash == after_hash:
            self.add_log(f"[同步] TX2 已套用，忽略重送\n{sender} -> {receiver} ({amount})")
            return True

        if my_hash != prev_hash:
            self._request_sync_from_node(
                initiator_id,
                f"偵測到交易序列缺口（本地 {my_hash[:12]}...，預期 {prev_hash[:12]}...）",
            )
            return True

        try:
            self._execute_transaction(sender, receiver, amount)
            new_hash = self._get_last_block_hash()
            if new_hash != after_hash:
                self._request_sync_from_node(
                    initiator_id,
                    f"交易套用後 hash 不一致（本地 {new_hash[:12]}...，預期 {after_hash[:12]}...）",
                )
            else:
                self.add_log(f"[同步] 收到可靠交易\n{sender} -> {receiver} ({amount})")
        except ValueError as e:
            self.add_log(
                f"[同步] TX2 套用失敗 {sender}->{receiver}({amount})\n原因: {e}"
            )
            self._request_sync_from_node(initiator_id, "可靠交易套用失敗")
        return True

    def _execute_client_transaction(self, sender, receiver, amount, tx_id=None):
        if not tx_id:
            prev_hash, after_hash = self._execute_transaction_with_receipt(sender, receiver, amount)
            self._broadcast_transaction(sender, receiver, amount, prev_hash, after_hash)
            return {"prev_hash": prev_hash, "after_hash": after_hash}

        owner = False
        while True:
            with self.tx_id_lock:
                cached = self.tx_id_results.get(tx_id)
                if cached is not None:
                    ok, payload = cached
                    if ok:
                        return payload
                    raise ValueError(payload["message"])

                event = self.tx_id_inflight.get(tx_id)
                if event is None:
                    event = threading.Event()
                    self.tx_id_inflight[tx_id] = event
                    owner = True
                    break

            event.wait(timeout=15)

        try:
            prev_hash, after_hash = self._execute_transaction_with_receipt(sender, receiver, amount)
            result = {"tx_id": tx_id, "prev_hash": prev_hash, "after_hash": after_hash}
            self._broadcast_transaction(sender, receiver, amount, prev_hash, after_hash)
            with self.tx_id_lock:
                self.tx_id_results[tx_id] = (True, result)
            return result
        except ValueError:
            # 失敗的交易不快取，避免暫時性凍結/修復中的錯誤把同一 tx_id 永久釘死。
            raise
        finally:
            if owner:
                with self.tx_id_lock:
                    event = self.tx_id_inflight.pop(tx_id, None)
                    if event:
                        event.set()

    def _execute_transaction(self, sender, receiver, amount):
        # 0. SYSTEM 交易（共識成功後的獎勵）不受信任凍結影響；其餘必須通過信任檢查
        if sender != "SYSTEM":
            ok, msg = self._require_network_trust("交易")
            if not ok:
                raise ValueError(msg)
        # 1. 如果是系統發錢 (SYSTEM)，不用檢查餘額
        if sender != "SYSTEM":
            # 2. 先呼叫我們剛才寫好的 checkMoney 查一下這個人剩多少錢
            res = self._execute_checkMoney(sender)
            # --- 這是取代 'NULL' 的黃金邏輯 ---
            if res is None:
                # 這裡主動觸發廣播（保險起見），並告訴使用者正在修復
                raise ValueError(f"發送者 {sender} 帳本異常\n已發起同步，請稍後重試")
            current_balance = res
            # 3. 檢查錢夠不夠
            if int(current_balance) < int(amount):
                raise ValueError(f"餘額不足！{sender} 目前只有 {current_balance} 元")
        
        tx_data = f"{sender}, {receiver}, {amount}\n"
        with self.file_lock:
            is_valid, msg = self._check_chain_unlocked()
            if not is_valid:
                raise ValueError(f"無法追加交易，因為本地帳本無效：{msg}")

            files = self._ledger_files_unlocked()
            if not files: 
                curr_id, curr_path = 1, f"{STORAGE_PATH}/1.txt"
                with open(curr_path, "w") as f: f.write("Sha256 of previous block: 0\nNext block: None\n")
            else:
                curr_id, curr_path = int(files[-1].split('.')[0]), f"{STORAGE_PATH}/{files[-1]}"

            with open(curr_path, "r") as f: lines = f.readlines()
            if sum(1 for l in lines if "," in l) < 5:
                with open(curr_path, "a") as f: f.write(tx_data)
            else:
                new_id, new_path = curr_id + 1, f"{STORAGE_PATH}/{curr_id + 1}.txt"
                for i, line in enumerate(lines):
                    if line.startswith("Next block:"): lines[i] = f"Next block: {new_id}.txt\n"
                with open(curr_path, "w") as f: f.writelines(lines)
                prev_hash = self._get_file_hash(curr_path)
                with open(new_path, "w") as f: f.write(f"Sha256 of previous block: {prev_hash}\nNext block: None\n{tx_data}")
            self._write_head_hash_unlocked()
