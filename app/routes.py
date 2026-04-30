#================================
# M1421070 戴弘奕；M1429012 吳承翰  
#================================
from flask import Blueprint, render_template, request, jsonify

main_bp = Blueprint('main', __name__)

def init_routes(node):
    global node_instance
    node_instance = node
    return main_bp

@main_bp.route('/')

def index():
    # 根據 Docker Compose 設定的 IP 自動判斷 Client 名稱
    ip_mapping = {
        "100.122.78.117:8001": "Node 4",
        "100.122.78.117:8002": "Node 5",
        "100.122.78.117:8003": "Node 6",

        # VM
        "100.119.224.86:8001": "Node 1",
        "100.119.224.86:8002": "Node 2",
        "100.119.224.86:8003": "Node 3"
    }
    current_identity = f"{node_instance.ip}:{node_instance.port}"
    node_name = ip_mapping.get(current_identity, f"Node ({current_identity})")
    return render_template("index.html", ip=current_identity, node_name=node_name)

@main_bp.route('/api/money/<account>')
def api_check_money(account):
    return jsonify({"balance": node_instance._execute_checkMoney(account, gui_mode=True)})

@main_bp.route('/api/log/<account>')
def api_check_log(account):
    return jsonify({"logs": node_instance._execute_checkLog(account, gui_mode=True)})

@main_bp.route('/api/transaction', methods=['POST'])
def api_transaction():
    try:
        data = request.json
        # 呼叫執行交易
        node_instance._execute_transaction(data['sender'], data['receiver'], data['amount'])
        
        # 成功才廣播
        tx_msg = f"TX:{data['sender']}:{data['receiver']}:{data['amount']}"
        for peer in node_instance.peers:
            node_instance.sock.sendto(tx_msg.encode('utf-8'), peer)
            
        return jsonify({"status": "success", "message": "交易成功"}), 200
        
    except ValueError as e:
        # 捕捉餘額不足的錯誤
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        print(f"🚨 系統崩潰！原因: {e}")
        return jsonify({"status": "error", "message": "系統發生未知錯誤"}), 500

@main_bp.route('/api/checkChain')
def api_check_chain():
    is_valid, msg = node_instance._execute_checkChain(gui_mode=True, auto_repair=True)
    return jsonify({"status": is_valid, "message": msg})

@main_bp.route('/api/checkAllChains/<target>')
def api_check_all_chains(target):
    return jsonify({"message": node_instance._execute_checkAllChains(target, gui_mode=True)})

@main_bp.route('/api/poll_logs')
def api_poll_logs():
    with node_instance.log_lock:
        logs = node_instance.log_buffer.copy()
        node_instance.log_buffer.clear()
    return jsonify({"logs": logs})
