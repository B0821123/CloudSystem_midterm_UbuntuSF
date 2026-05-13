#================================
# M1421070 戴弘奕；M1429012 吳承翰  
#================================
import urllib.request
import json
import random
import time
import sys
import uuid
import urllib.error

# 使用本機 loopback，不受 VM IP 變動影響
URL = "http://127.0.0.1:8081/api/transaction"
HEALTH = "http://127.0.0.1:8081/"

# 建立一些假帳戶名稱來做交易測試
USERS = ['Darren', 'Alice', 'Bob', 'Charlie', 'Eve']

RUN_ID = f"demo-{int(time.time())}-{uuid.uuid4().hex[:8]}"
REQUEST_TIMEOUT = 8
MAX_RETRIES = 3


def post_transaction(sender, receiver, amount, tx_id, label):
    payload = json.dumps({
        "tx_id": tx_id,
        "sender": sender,
        "receiver": receiver,
        "amount": amount,
    }).encode('utf-8')

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(
            URL,
            data=payload,
            headers={'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                json.loads(response.read().decode('utf-8'))
                if attempt > 1:
                    print(f"{label} 🔁 retry {attempt} recovered")
                return True
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            last_error = f"HTTP {e.code}: {body}"
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(0.5 * attempt)

    print(f"{label} ❌ 轉帳失敗: {last_error}")
    return False

# 健康檢查：等 Client 1 的 Web GUI 真的起來再開打
print("⏳ 正在等待 Client 1 的 Web GUI 就緒 ...")
READY = False
for attempt in range(30):
    try:
        with urllib.request.urlopen(HEALTH, timeout=1) as resp:
            if resp.status == 200:
                READY = True
                break
    except Exception:
        pass
    time.sleep(1)

if not READY:
    print("❌ 等待逾時：Client 1 無法連線，請先確認 docker-compose up -d 已成功啟動容器。")
    sys.exit(1)

print("✅ Client 1 已就緒，執行系統創世發錢 (SYSTEM Airdrop) ...")

for user in USERS:
    label = f"[SYSTEM] -> {user:<8} $50000"
    if post_transaction("SYSTEM", user, 50000, f"{RUN_ID}-airdrop-{user}", label):
        print(f"{label} ✅")
    
    time.sleep(0.1) # 給節點一點緩衝時間

print("-" * 50)
print("SYSTEM 初始金額發放完畢，現在開始跑 100 筆隨機交易...")

success_count = 0
fail_count = 0

for i in range(1, 101):
    # 隨機挑選付款人與收款人
    sender = random.choice(USERS)
    receiver = random.choice([u for u in USERS if u != sender])
    amount = random.randint(50, 500)

    label = f"[{i:3d}/100] {sender:<8} -> {receiver:<8} ${amount:>3}"
    if post_transaction(sender, receiver, amount, f"{RUN_ID}-tx-{i:03d}", label):
        success_count += 1
        print(f"{label} ✅")
    else:
        fail_count += 1

    # 稍微暫停 0.05 秒，避免網路塞車且讓節點有時間同步與寫入檔案
    time.sleep(0.05)

print("\n" + "=" * 50)
print(f"結果統計：成功 {success_count} 筆 / 失敗 {fail_count} 筆")
print(f"預期區塊數: {success_count // 5} 個完整區塊 + {success_count % 5} 筆在最新區塊")
print("=" * 50)
