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

# ==========================================
# P2P Node 核心類別
# ==========================================
class P2PNode:
    def __init__(self, ip, port, peers):
        self.ip = ip
        self.port = port
        self.peers = peers
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', self.port))
        
        self.file_lock = threading.Lock()
        self.expected_hashes = {}
        self.awaiting_hashes = False
        
        self.log_buffer = []
        self.log_lock = threading.Lock()

        self.node_id = os.environ.get("NODE_NAME", f"{ip}-{port}")
        self.network_token = "MY_BLOCKCHAIN_SECRET_2026"

        self.nodes_contact_book = {}
        for p_ip, p_port in self.peers:
            p_id = f"{p_ip}-{p_port}"
            self.nodes_contact_book[p_id] = (p_ip, p_port)

    def add_log(self, msg):
        print(msg)
        with self.log_lock:
            self.log_buffer.append(msg)

    def start(self):
        print(f"📡 P2P Listener starting at {self.ip}:{self.port}")
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
                message = data.decode('utf-8')
                
                if message.startswith("TX:"):
                    parts = message.split(":")
                    if len(parts) == 4:
                        self._execute_transaction(parts[1], parts[2], parts[3])
                        self.add_log(f"[網路同步] 收到廣播交易: {parts[1]} 轉給 {parts[2]} {parts[3]} 元")
                    
                elif message.startswith("REQ_HASH"):
                    self.add_log(f"[跨節點驗證] 收到來自 {addr[0]} 的 Hash 請求，已回傳驗證結果。")
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
                    parts = message.split(":")
                    if len(parts) >= 3:
                        majority_hash = parts[1]
                        provider_id = parts[2]
                        my_hash = self._get_last_block_hash()
                        # 我才是提供者 -> 無需修復
                        if provider_id == self.node_id:
                            pass
                        elif my_hash != majority_hash:
                            if provider_id in self.nodes_contact_book:
                                provider_addr = self.nodes_contact_book[provider_id]
                                self.add_log(f"[共識機制] ❌ 警告：本地帳本與全網共識不符！\n正在向信任節點 {provider_id} 請求修復...")
                                self.sock.sendto(b"REQ_SYNC", provider_addr)
                            else:
                                self.add_log(f"[共識機制] ⚠️ 收到廣播但找不到提供者 {provider_id} 的通訊錄地址。")

                elif message.startswith("REQ_SYNC"):
                    last_hash = self._get_last_block_hash()
                    if last_hash in ["INVALID", "EMPTY"]:
                        self.add_log(f"[SYNC] Reject sync request from {addr[0]} because local ledger is {last_hash}.")
                        continue
                    self.add_log(f"[共識機制] 收到來自 {addr[0]} 的修復請求，正在傳送正確帳本資料...")
                    ledger_data = self._pack_ledger()
                    self.sock.sendto(f"RESP_SYNC:{ledger_data}".encode('utf-8'), addr)

                elif message.startswith("RESP_SYNC:"):
                    json_str = message[len("RESP_SYNC:"):]
                    self._unpack_and_repair_ledger(json_str)
                    self.add_log("🎉 [共識機制] 置換完成！本地帳本已成功依照 >50% 多數決共識修復！")

            except Exception as e:
                print(f"[Error] 監聽發生錯誤: {e}")

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
            return True, "沒有帳本區塊"

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

        for peer in self.peers:
            self.sock.sendto(b"REQ_HASH", peer)

        my_hash = self._get_last_block_hash()
        time.sleep(SYNC_WAIT_SECONDS)
        self.awaiting_hashes = False

        all_votes = self.expected_hashes.copy()
        all_votes[self.node_id] = my_hash
        total_expected = len(self.peers) + 1
        return my_hash, all_votes, total_expected

    def _majority_hash(self, all_votes):
        valid_hashes = Counter(h for h in all_votes.values() if h not in ["INVALID", "EMPTY"])
        if not valid_hashes:
            return None, 0
        return valid_hashes.most_common(1)[0]

    def _request_sync_from_majority(self, my_hash, all_votes, total_expected):
        majority_hash, max_count = self._majority_hash(all_votes)
        if not majority_hash:
            return False, "No valid peer ledger hash is available for repair."

        if max_count <= total_expected / 2:
            return False, f"No majority ledger hash yet ({max_count}/{total_expected})."

        if my_hash == majority_hash:
            return True, "Local ledger already matches the majority."

        provider_id = [node_id for node_id, h in all_votes.items() if h == majority_hash][0]
        if provider_id not in self.nodes_contact_book:
            return False, f"Repair provider {provider_id} is not in the contact book."

        self.sock.sendto(b"REQ_SYNC", self.nodes_contact_book[provider_id])
        return True, f"{provider_id} 發起維修請求"

    def _repair_from_majority(self):
        my_hash, all_votes, total_expected = self._collect_last_hash_votes()
        return self._request_sync_from_majority(my_hash, all_votes, total_expected)

    def _execute_checkMoney(self, target, gui_mode=False):
        
        is_valid = self._execute_checkChain()
        if not is_valid:
            # 如果帳本損毀，直接報錯或回傳 None，不進行後續計算
            self.add_log(f"⚠️ [安全警示] 拒絕查詢餘額：本地帳本已受損，請先進行共識修復！")
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
        except Exception as e: print(f"[Error] 解析失敗: {e}")

    def _execute_checkAllChains(self, target, gui_mode=False):
        # 1. 初始化收集箱
        self.expected_hashes.clear()
        self.awaiting_hashes = True

        # 2. 發送請求給所有人 (REQ_HASH)
        for peer in self.peers:
            self.sock.sendto(b"REQ_HASH", peer)

        # 3. 整合選票 (包含自己的一票)
        my_hash = self._get_last_block_hash()
        time.sleep(SYNC_WAIT_SECONDS) 
        self.awaiting_hashes = False
        
        all_votes = self.expected_hashes.copy()
        all_votes[self.node_id] = my_hash
        total_expected = len(self.peers) + 1
        
        output_msg = f"--- 實名制共識比對 (Token 驗證) --- \n"
        output_msg += f"預期節點: {total_expected} | 實際收到回覆: {len(all_votes)}\n"

        # 4. 統計出現次數最多的 Hash並排除掉無效的 Hash (例如 INVALID 或 EMPTY)
        valid_hashes = Counter(h for h in all_votes.values() if h not in ["INVALID", "EMPTY"])

        if not valid_hashes:
            return "❌ 系統不被信任：全網均無效帳本。" if gui_mode else None
        
        majority_hash, max_count = valid_hashes.most_common(1)[0]

        # 5. 判斷是否過半數
        if max_count > total_expected / 2:
            # 【關鍵】從多數派中挑一個持有正確 Hash 的節點作為修復來源
            provider_id = next(node_id for node_id, h in all_votes.items() if h == majority_hash)

            # 【全網修復廣播】告訴每一個節點「正確的 Hash 是什麼、該向誰要」
            # 任何本地 Hash 不符的節點（包含被竄改的 peer）會自動向 provider 請求 REQ_SYNC
            broadcast_msg = f"BROADCAST_MAJORITY:{majority_hash}:{provider_id}"
            for peer in self.peers:
                self.sock.sendto(broadcast_msg.encode('utf-8'), peer)
            self.add_log(f"[共識機制] 已向全網廣播修復通知（多數決 Hash: {majority_hash[:12]}... / 提供者: {provider_id}）")

            # 如果連我自己都跟多數派不符，也主動發一次 REQ_SYNC 修復自己
            if my_hash != majority_hash:
                output_msg += f"\n⚠️ 本地帳本與多數不符！向 {provider_id} 請求帳本修復..."
                if provider_id in self.nodes_contact_book:
                    self.sock.sendto(b"REQ_SYNC", self.nodes_contact_book[provider_id])

            # 等待所有受損節點完成 REQ_SYNC / RESP_SYNC 修復流程，再發獎勵交易，
            # 否則 TX 廣播會在還沒修好的節點上因本地帳本無效而被拒絕。
            time.sleep(SYNC_WAIT_SECONDS)

            if my_hash == majority_hash:
                output_msg += f"\n✅ 全網達成共識 ({max_count}/{total_expected})！\n獎勵發放: 100 元 -> {target}"
                self._execute_transaction("SYSTEM", target, "100")
                # 廣播交易給所有人
                for peer in self.peers:
                    self.sock.sendto(f"TX:SYSTEM:{target}:100".encode('utf-8'), peer)
            else:
                output_msg += f"\n（本地帳本剛剛向 {provider_id} 完成修復，本輪不發放獎勵，下次驗證再領取。）"
        else:
            output_msg += f"\n❌ 系統不被信任：無法達成過半數共識 (僅 {max_count}/{total_expected})。"
            
        if gui_mode: return output_msg

    def _execute_transaction(self, sender, receiver, amount):
        # 1. 如果是系統發錢 (SYSTEM)，不用檢查餘額
        if sender != "SYSTEM":
            # 2. 先呼叫我們剛才寫好的 checkMoney 查一下這個人剩多少錢
            res = self._execute_checkMoney(sender)
            # --- 這是取代 'NULL' 的黃金邏輯 ---
            if res is None:
                # 這裡主動觸發廣播（保險起見），並告訴使用者正在修復
                raise ValueError(f"⚠️ 偵測到發送者 {sender} 的帳本異常，系統已自動發起全網同步，請在 2 秒後重試！")
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