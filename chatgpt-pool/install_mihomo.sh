#!/data/data/com.termux/files/usr/bin/bash
# Termux 依赖安装（Mihomo + Python，均为标准库代码，无需 pip 包）
set -e
echo "[1/2] pkg update"
pkg update -y
echo "[2/2] install mihomo + python"
pkg install -y mihomo python
echo "OK. 下一步: cp -r chatgpt-pool ~/chatgpt-pool && cd ~/chatgpt-pool && python3 main.py init"
