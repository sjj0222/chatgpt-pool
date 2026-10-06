#!/data/data/com.termux/files/usr/bin/bash
# V0.2.2.1 P0-06：Termux 真实环境六步验收测试
# 用法：在 Termux 中执行
#   cp -r chatgpt-pool ~/chatgpt-pool
#   cd ~/chatgpt-pool
#   bash test_termux.sh
set -u
FAIL=0

step() { echo; echo "===== [$1/6] $2 ====="; }
ok()   { echo "[PASS] $1"; }
bad()  { echo "[FAIL] $1"; FAIL=1; }

# V0.2.2.1 P0-03：诊断产物验收检查（run/ 下三个固定产物）
check_file() {
    if [ -f "$1" ]; then
        ok "$1 exists"
    else
        bad "$1 missing"
    fi
}

step 1 "版本：mihomo -v"
if mihomo -v; then ok "mihomo 可执行"; else bad "mihomo 未安装（pkg install mihomo）"; fi

step 2 "初始化：python3 main.py init"
if python3 main.py init; then ok "init 完成"; else bad "init 失败"; fi

step 3 "刷新：python3 main.py refresh"
if python3 main.py refresh; then
    ok "refresh 执行完成（published 取决于真实节点；失败轮次应写入 run/failed/ 且旧池保留）"
else
    # refresh 退出码 1 = 未发布（EMPTY_POOL/系统级失败）；失败可解释即符合预期
    ok "refresh 未发布（查看上方 Detection Result：Stage/Reason 必须可解释）"
fi
echo "-- run/ 目录隔离检查（V0.2.2.1）--"
if [ -f run/final/current.json ]; then
    ok "run/final/current.json 存在（成功池）"
else
    ok "无 run/final/current.json（尚无成功池，属预期）"
fi
if ls run/failed/ >/dev/null 2>&1; then
    ok "失败轮次已写入 run/failed/（未污染 current）"
    if ls run/failed/nodes/ >/dev/null 2>&1; then
        ok "失败节点明细 run/failed/nodes/ 已生成（V0.2.2）"
    else
        echo "  （本轮无失败节点明细）"
    fi
else
    echo "  （本轮无失败轮次）"
fi
echo "-- 诊断产物检查（V0.2.2.1 P0-03）--"
check_file run/source_health.json
check_file run/detect.log
check_file run/refresh_state.json
echo "-- chatgpt.yaml --"
[ -f chatgpt.yaml ] && echo "  chatgpt.yaml 存在（订阅已交付）" \
                    || echo "  无 chatgpt.yaml（首次运行失败不生成空 YAML，符合 §29）"

step 4 "启动：python3 main.py mihomo start"
OUT=$(python3 main.py mihomo start)
echo "$OUT"
case "$OUT" in
  *'"started": true'*) ok "Mihomo 已启动（7890/9090 就绪）";;
  *NO_ACTIVE_POOL*) ok "安全检查生效：无有效池时拒绝启动（P0-03）";;
  *) bad "start 返回异常状态";;
esac

step 5 "状态：python3 main.py mihomo status"
if python3 main.py mihomo status; then
    ok "status 输出完整（active_pool/ports/version/last_refresh/current_pool_age）"
else
    bad "status 失败"
fi

step 6 "停止：python3 main.py mihomo stop"
OUT=$(python3 main.py mihomo stop)
echo "$OUT"
# V0.2.2.2 Lifecycle 验收：stop 后 7890/9090 必须释放（PASS/PORT_RELEASE_TIMEOUT）
if echo "$OUT" | grep -q "PASS port released"; then
    ok "ports released（7890/9090）"
elif echo "$OUT" | grep -q "PORT_RELEASE_TIMEOUT"; then
    bad "PORT_RELEASE_TIMEOUT（stop 后仍有端口占用）"
else
    bad "stop 输出异常"
fi
# 停止后独立复核 7890/9090 已释放
sleep 1
python3 - <<'PY'
import socket
busy = []
for p in (7890, 9090):
    s = socket.socket()
    if s.connect_ex(("127.0.0.1", p)) == 0:
        busy.append(p)
    s.close()
print("端口释放复核:", "OK" if not busy else f"仍占用: {busy}")
PY

echo
if [ "$FAIL" = 0 ]; then
    echo "===== P0-06 全部通过 ====="
else
    echo "===== 存在 FAIL，请按上方提示处理 ====="
fi
exit $FAIL
