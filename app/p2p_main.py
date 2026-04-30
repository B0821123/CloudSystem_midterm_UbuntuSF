#================================
# M1421070 戴弘奕；M1429012 吳承翰  
#================================
import os
from flask import Flask
import logging
from blockchain import P2PNode
from routes import init_routes

# 隱藏 Flask 開機與運作時的煩人日誌
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

STORAGE_PATH = "/storage"

app = Flask(__name__)

# 全網通訊錄：填入節點的大名單
ALL_NODES = {
    "NODE_4":("100.122.78.117", 8001), 
    "NODE_5":("100.122.78.117", 8002),
    "NODE_6":("100.122.78.117", 8003),
    "NODE_1":("100.119.224.86", 8001),
    "NODE_2":("100.119.224.86", 8002),
    "NODE_3":("100.119.224.86", 8003)
}

if __name__ == '__main__':
    # 1. 身份定義：手動填入你的 Tailscale IP
    my_ip = "100.119.224.86" 
    my_port = int(os.environ.get("MY_P2P_PORT", 8001))
    my_addr = (my_ip, my_port)
     
    # 2. 過濾邏輯：自動算出「我要連向誰」 (這行邏輯可以保留)
    peers = [addr for addr in ALL_NODES.values() if addr != my_addr]
    
    # 3. 啟動節點
    node_instance = P2PNode(my_ip, my_port, peers)
    node_instance.start()
    app.register_blueprint(init_routes(node_instance))
    
    # 4. 啟動 Flask 控制台
    print("🌐 啟動 Web GUI Server (Port: 5000)...")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)