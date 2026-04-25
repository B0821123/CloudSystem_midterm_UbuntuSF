FROM ubuntu:22.04

# 設定環境變數避免安裝時卡在時區設定
ENV DEBIAN_FRONTEND=noninteractive

# 更新並安裝必備套件
# - python3 / python3-pip / python3-flask：執行 P2P 節點與 Web GUI
# - vim / net-tools / iputils-ping：Demo 現場除錯與進容器觀察用
# - hostname：程式用 `hostname -i` 取得容器 IP
# - curl：方便用命令列打 API 測試
RUN apt-get update && apt-get install -y \
    vim \
    net-tools \
    python3 \
    python3-pip \
    python3-flask \
    iputils-ping \
    hostname \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 預設指令由 docker-compose.yml 的 command 覆寫
CMD ["tail", "-f", "/dev/null"]
