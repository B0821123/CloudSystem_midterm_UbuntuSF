#================================
# M1421070 戴弘奕；M1429012 吳承翰  
#================================
from flask import Blueprint, render_template, request, jsonify

main_bp = Blueprint('main', __name__)

def init_routes(node):
    global node_instance
    node_instance = node
    return main_bp

# 全網節點顯示名稱對照表（key = node_id，與 P2PNode.node_id / NODE_NAME 同源）
# 同 host peer 的 ip 欄位現在會是 docker 服務名（client1/2/3），不再是 Tailscale IP，
# 因此改用穩定的 node_id 來查顯示名，避免內外位址表示不一致時對不上。
NODE_NAME_MAPPING = {
    "NODE_1": "Node 1",
    "NODE_2": "Node 2",
    "NODE_3": "Node 3",
    "NODE_4": "Node 4",
    "NODE_5": "Node 5",
    "NODE_6": "Node 6",
}


def _display_name_by_id(node_id, fallback_ip=None, fallback_port=None):
    if node_id in NODE_NAME_MAPPING:
        return NODE_NAME_MAPPING[node_id]
    if fallback_ip is not None and fallback_port is not None:
        return f"{fallback_ip}:{fallback_port}"
    return node_id


@main_bp.route('/')
def index():
    current_identity = f"{node_instance.ip}:{node_instance.port}"
    node_name = _display_name_by_id(node_instance.node_id, node_instance.ip, node_instance.port)
    return render_template("index.html", ip=current_identity, node_name=node_name)


@main_bp.route('/api/peers')
def api_peers():
    peers = node_instance.get_peer_status()
    # 自己永遠在線
    self_entry = {
        "node_id": node_instance.node_id,
        "ip": node_instance.ip,
        "port": node_instance.port,
        "online": True,
        "is_self": True,
        "name": _display_name_by_id(node_instance.node_id, node_instance.ip, node_instance.port),
        "last_seen_ago": 0,
    }
    enriched = [self_entry]
    for p in peers:
        p["name"] = _display_name_by_id(p["node_id"], p["ip"], p["port"])
        p["is_self"] = False
        enriched.append(p)
    enriched.sort(key=lambda x: x["name"])
    online_count = sum(1 for p in enriched if p["online"])
    return jsonify({
        "peers": enriched,
        "online_count": online_count,
        "total": len(enriched),
        "network_trusted": node_instance.network_trusted,
        "network_trusted_reason": node_instance.network_trusted_reason,
    })

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
        result = node_instance._execute_client_transaction(
            data['sender'],
            data['receiver'],
            data['amount'],
            data.get('tx_id'),
        )

        return jsonify({"status": "success", "message": "交易成功", **result}), 200
        
    except ValueError as e:
        # 捕捉餘額不足的錯誤
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        print(f"[ERROR] {e}")
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

@main_bp.route('/api/stats')
def get_stats():
    return jsonify({"total_tx": node_instance._get_total_tx_count()})
