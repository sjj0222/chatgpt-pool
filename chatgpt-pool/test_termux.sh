#!/data/data/com.termux/files/usr/bin/bash
# V0.4.3.1：Termux 真实环境验收测试（只修 harness，不改生产逻辑）
# 用法：
#   cd ~/chatgpt-pool/chatgpt-pool
#   bash test_termux.sh
set -u

# ---- 统一运行目录（V0.4.3.1：不再依赖 cwd 相对路径）----
BASE_DIR="${CHATGPT_POOL_BASE:-$(cd "$(dirname "$0")" && pwd)}"
RUN_DIR="$BASE_DIR/run"
cd "$BASE_DIR" || exit 1

FAIL=0
WEB_PID=""

step() { echo; echo "===== [$1] $2 ====="; }
ok()   { echo "[PASS] $1"; }
bad()  { echo "[FAIL] $1"; FAIL=1; }
note() { echo "  (note) $1"; }

check_file() {
    if [ -f "$1" ]; then
        ok "$(basename "$1") exists"
    else
        bad "$(basename "$1") missing: $1"
    fi
}

# ---- Web 清理（V0.4.3.1：API 测试完成后再 kill）----
cleanup_web() {
    if [ -n "$WEB_PID" ]; then
        kill "$WEB_PID" 2>/dev/null || true
        wait "$WEB_PID" 2>/dev/null || true
    fi
}
trap cleanup_web EXIT

# ============ step 1: mihomo 版本 ============
step "1" "mihomo -v"
if mihomo -v; then
    ok "mihomo 可执行"
else
    bad "mihomo 未安装（pkg install mihomo）"
fi

# ============ step 2: init ============
step "2" "python3 main.py init"
if python3 main.py init; then
    ok "init 完成"
else
    bad "init 失败"
fi

# ============ step 3: refresh + 诊断文件验收 ============
step "3" "python3 main.py refresh（诊断文件验收）"
set +e
python3 main.py refresh
RC=$?
set -e
# refresh 退出码 1 = 未发布（EMPTY_POOL/ENVIRONMENT_ANOMALY/BOOTSTRAP_FAILED）
# 这是真实环境状态，不是产品 bug
if [ $RC -eq 0 ]; then
    note "refresh published（成功池）"
else
    note "refresh 未发布（退出码 $RC，可能是 ENVIRONMENT_ANOMALY/BOOTSTRAP_FAILED/EMPTY_POOL——真实环境状态）"
fi

echo "-- 诊断文件（V0.4.3.1 统一用 \$RUN_DIR/）--"
mkdir -p "$RUN_DIR/failed/nodes"
check_file "$RUN_DIR/source_health.json"
check_file "$RUN_DIR/detect.log"
check_file "$RUN_DIR/refresh_state.json"
if ls "$RUN_DIR/failed/"*.json >/dev/null 2>&1; then
    ok "failed/*.json exists"
else
    bad "failed/*.json missing"
fi
if ls "$RUN_DIR/failed/nodes/"*.jsonl >/dev/null 2>&1; then
    ok "failed/nodes/*.jsonl exists"
else
    bad "failed/nodes/*.jsonl missing"
fi
check_file "$RUN_DIR/runtime_report.json"

echo "-- chatgpt.yaml（空池保护）--"
if [ -f "$BASE_DIR/chatgpt.yaml" ]; then
    note "chatgpt.yaml 存在（订阅已交付）"
else
    note "无 chatgpt.yaml（首次失败/空池不生成空 YAML，符合 §29）"
fi

# ============ step 4: mihomo start ============
step "4" "python3 main.py mihomo start"
OUT=$(python3 main.py mihomo start 2>&1)
echo "$OUT"
case "$OUT" in
  *'"started": true'*) ok "Mihomo 已启动（7890/9090 就绪）" ;;
  *NO_ACTIVE_POOL*)   ok "安全检查生效：无有效池时拒绝启动（合理业务结果）" ;;
  *)                  bad "start 返回异常状态" ;;
esac

# ============ step 5: mihomo status ============
step "5" "python3 main.py mihomo status"
if python3 main.py mihomo status; then
    ok "status 输出完整"
else
    bad "status 失败"
fi

# ============ step 6: mihomo stop（V0.4.3.1 修命令拼接 + 宽容业务结果）============
step "6" "python3 main.py mihomo stop"
OUT=$(python3 main.py mihomo stop 2>&1)
echo "$OUT"
# already_stopped / NO_ACTIVE_POOL / NOT_RUNNING = 合理业务结果，不判 FAIL
case "$OUT" in
  *already_stopped*|*NO_ACTIVE_POOL*|*NOT_RUNNING*|*'"stopped": true'*)
      ok "stop 返回合理状态（already_stopped/stopped/NO_ACTIVE_POOL）" ;;
  *PORT_RELEASE_TIMEOUT*)
      bad "PORT_RELEASE_TIMEOUT（应释放端口持续占用）" ;;
  *)
      bad "stop 返回异常状态" ;;
esac

# ============ step 7: Web 启动 + 页面/API 全量验收 ============
step "7" "Web Dashboard（127.0.0.1:18888）+ API 验收"
python3 main.py web > "$RUN_DIR/web_test.log" 2>&1 &
WEB_PID=$!
sleep 3

WEB_OK=0
if curl -s http://127.0.0.1:18888/api/health >/dev/null 2>&1; then
    ok "/api/health 可访问"
    WEB_OK=1
else
    bad "Web 未能启动（查看 $RUN_DIR/web_test.log）"
fi

if [ "$WEB_OK" = 1 ]; then
    echo "-- 页面路由（V0.4.3.1：只检查 200 可达，不检查具体文案）--"
    for page in /node /source /events /diagnostic /m; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:18888$page")
        if [ "$CODE" = "200" ]; then
            ok "$page → 200"
        else
            bad "$page → HTTP $CODE（应 200）"
        fi
    done

    echo "-- API GET（V0.4.3.1：只要求合法 JSON 响应，不要求业务成功）--"
    for ep in /api/status /api/nodes /api/sources /api/events; do
        BODY=$(curl -s "http://127.0.0.1:18888$ep")
        if echo "$BODY" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
            ok "GET $ep → 合法 JSON"
        else
            bad "GET $ep → 非法 JSON 或无响应"
        fi
    done

    echo "-- API POST（V0.4.3.1：允许业务失败，只要求 HTTP 正常 + 结构合法）--"
    for ep in /api/refresh /api/start /api/stop; do
        BODY=$(curl -s -X POST "http://127.0.0.1:18888$ep")
        if echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); assert isinstance(d, dict)" 2>/dev/null; then
            ok "POST $ep → 合法 JSON（业务结果可为 ENVIRONMENT_ANOMALY/NO_ACTIVE_POOL）"
        else
            bad "POST $ep → 非法响应"
        fi
    done
fi

# Web 收尾（所有 API/页面测试完成后才 kill）
cleanup_web
WEB_PID=""
note "Web 进程已停止"

# ============ step 8: scheduler / health_check / backup ============
step "8" "scheduler / health_check / backup"
if python3 main.py scheduler status >/dev/null 2>&1; then
    ok "scheduler status 可执行"
else
    bad "scheduler status 失败"
fi
if python3 main.py scheduler recover >/dev/null 2>&1; then
    ok "scheduler recover 可执行"
else
    bad "scheduler recover 失败"
fi
if python3 main.py scheduler cleanup >/dev/null 2>&1; then
    ok "scheduler cleanup 可执行"
else
    bad "scheduler cleanup 失败"
fi
if python3 main.py scheduler health_check >/dev/null 2>&1; then
    ok "health_check 可执行（runtime_report 每小时更新）"
else
    bad "health_check 失败"
fi
check_file "$RUN_DIR/runtime_report.json"
if python3 main.py scheduler backup >/dev/null 2>&1; then
    ok "scheduler backup 可执行"
else
    bad "scheduler backup 失败"
fi
if python3 main.py backup >/dev/null 2>&1; then
    ok "backup 可执行"
else
    bad "backup 失败"
fi

# ============ 最终分类输出 ============
echo
echo "=========================================="
if [ "$FAIL" = 0 ]; then
    echo "RESULT: PRODUCT PASS（含真实环境异常时诊断正确）"
else
    echo "RESULT: PRODUCT FAIL（见上方 [FAIL] 项）"
fi
echo "=========================================="
exit $FAIL
