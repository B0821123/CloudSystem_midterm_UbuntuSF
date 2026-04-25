#!/bin/bash
set -e

echo "========================================"
echo "🧹 [1/4] 清理舊帳本資料 ..."
echo "========================================"
rm -f ./storage/client1/*.txt
rm -f ./storage/client2/*.txt
rm -f ./storage/client3/*.txt

echo ""
echo "========================================"
echo "🐳 [2/4] 啟動 Docker 容器並自動執行 P2P 節點 ..."
echo "========================================"
docker-compose down
docker-compose up -d --build

echo ""
echo "========================================"
echo "⏳ [3/4] 等待節點就緒 ..."
echo "========================================"
# auto_tx.py 自己會做 30 秒健康檢查重試，這裡只需簡短等候容器啟動
sleep 3

echo ""
echo "========================================"
echo "🚀 [4/4] 自動產生 100 筆測試交易 ..."
echo "========================================"
python3 auto_tx.py

echo ""
echo "========================================"
echo "✅ 環境建置完畢"
echo "========================================"
echo "在實體電腦瀏覽器開啟（替換成你的 VM IP）："
echo "  🔗 Client 1: http://192.168.244.128:8081"
echo "  🔗 Client 2: http://192.168.244.128:8082"
echo "  🔗 Client 3: http://192.168.244.128:8083"
echo ""
echo "常用操作："
echo "  👉 即時監看三節點日誌 : docker-compose logs -f"
echo "  👉 進入 Client 2 竄改  : docker exec -it client2 bash"
echo "  👉 停止整個環境        : docker-compose down"
echo "========================================"
