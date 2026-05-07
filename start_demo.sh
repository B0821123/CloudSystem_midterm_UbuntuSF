#!/bin/bash
# Demo bootstrap：清掉舊帳本 -> 起 docker -> 灌 100 筆測試交易
set -e

HOST_IP="${HOST_IP:-localhost}"

# 清除上一輪殘留的帳本
rm -f ./storage/client{1,2,3}/*.txt

# 重啟三個 P2P 節點
docker-compose down
docker-compose up -d --build

# 容器要幾秒才會把 Flask 拉起來，auto_tx.py 內部還會自己 retry
sleep 3

python3 auto_tx.py

cat <<EOF

Done. 從你的實體機瀏覽器連入：
  Client 1  http://${HOST_IP}:8081
  Client 2  http://${HOST_IP}:8082
  Client 3  http://${HOST_IP}:8083

常用指令：
  docker-compose logs -f          # 追蹤三個節點 log
  docker exec -it client2 bash    # 進 client2 做竄改測試
  docker-compose down             # 收掉整個環境
EOF
