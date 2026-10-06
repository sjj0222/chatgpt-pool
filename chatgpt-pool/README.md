# ChatGPT 免费公共节点池（Termux）— V5.2 + V5.2-P1 实现

在 Android + Termux 上本地运行：自动抓取免费公共代理节点 → 解析清洗去重 →
经 Mihomo 启动 → 真实出口 IP → 真实出口国家筛选 → ChatGPT 严格检测 →
稳定性复测（3 次，≥2/3）→ 原子替换最终池 → 定时刷新（7200s）→
`127.0.0.1:18787/chatgpt.yaml` 订阅交付。

> 规则基准：`chatgpt_pool_rules_v5.2.md`（正文 V5.2 + 补丁附录 V5.2-P1）。
> 实现完全按该基准；发现冲突先停止并报告，不自行改规则。

## 目录结构

```
~/chatgpt-pool/           生产目录（固定）
├── main.py                CLI：init / refresh / serve / run / check / mihomo
├── config.py              所有实现规格（endpoint/来源/端口/超时，P1-06/P1-07）
├── config.example.json    可编辑配置模板（init 时生成 config.json）
├── models.py              状态机 / chatgpt_status / failure_reason 枚举与优先级
├── parser.py / dedup.py   节点解析与 node_id 去重（§23）
├── countries.py           国家列表三级来源（§6 / P1-06）
├── fetchers.py            节点源抓取（来源是配置项）
├── engine.py              逐节点检测 Mihomo 引擎（生产/测试隔离，P1-08 / §33）
├── mihomo_svc.py          生产 Mihomo 常驻服务（start/stop/status/reload，PID 隔离）
├── history.py             节点历史/健康度/评分（V0.2.1 P1-01/02/03）
├── probe.py               HTTP 探测（经代理/直连，异常→§16 枚举）
├── egress.py              真实出口 IP → ISO Alpha-2（§5）
├── chatgpt_check.py       HOME/AUTH/BACKEND 严格检测（§10-14 / P1-01/P1-03/P1-04）
├── stability.py           稳定性复测固定 3 次（§20 / P1-02/P1-09/P1-10）
├── pipeline.py            刷新编排 + 发布门（§38 / P1-13/P1-14）
├── poolstore.py           raw/normalized/results/final + 原子替换（§24/§27）
├── server.py              订阅服务（仅 127.0.0.1，P1-08）
├── scheduler.py           定时刷新（发布后可回调热重载生产 Mihomo）
├── yaml_lite.py           零依赖 YAML 生成/校验（订阅 + Mihomo 配置）
├── supported_countries.json   国家列表本地缓存（自动生成）
├── raw/ normalized/ results/ state/ logs/ run/   运行数据（§24）
├── final/current.json     当前有效最终池（成功发布原子替换，V0.2.1 P0-02）
├── final/failed/          失败轮次诊断（绝不覆盖 current.json）
├── state/node_history.json    节点跨轮历史（P1-02）
├── logs/reject.log        REJECTED 明细（P1-04，与 pool.log/mihomo.log 分离）
└── chatgpt.yaml           最终订阅（原子替换产生）
```

测试目录 `~/chatgpt-pool-test/`：端口 18887 + 19000-19999，与生产完全隔离。

## 在 Termux 部署

```bash
pkg update -y && pkg install -y mihomo python     # 或执行 install_mihomo.sh
cp -r chatgpt-pool ~/chatgpt-pool
cd ~/chatgpt-pool
python3 main.py init        # 生成 config.json，可编辑节点源/endpoint
python3 main.py refresh     # 手动跑一轮
python3 main.py run         # 订阅服务(18787) + 定时刷新(7200s)，后台常驻
# 手机端代理客户端订阅地址：
#   http://127.0.0.1:18787/chatgpt.yaml
# （同一台手机内使用；如要局域网访问，自行改 server 绑定地址，风险自负）

# V0.2.1 P0-06：Termux 真实环境六步验收（mihomo -v / init / refresh / start / status / stop）
bash test_termux.sh
```

## V0.2.1 Production Hardening

- **P0-01 失败可解释**：`refresh` 结束输出 `reject_summary`（按本轮真实 REJECTED
  节点的 failure_reason 计数，来源是 pipeline 检测结果，不做猜测/二次扫描）。
- **P0-02 final 隔离**：成功发布 → `final/current.json`（原子替换）；失败轮次 →
  `final/failed/{round_id}.json`（含 reason/reject_summary 诊断），**绝不覆盖
  current.json**；系统级失败同样落 failed/。
- **P0-03 启动安全**：`mihomo start` 前检查 `final/current.json` 存在且节点数 > 0
  （否则 `NO_ACTIVE_POOL`，绝不启动空池）；配置生成成功；代理 7890 与 API 9090
  双端口就绪才返回 started。
- **P0-04 status 增强**：
  ```json
  { "running": true, "pid": 12345,
    "active_pool": { "count": 20, "round_id": "20261006..." },
    "ports": { "proxy": 7890, "api": 9090, "proxy_open": true, "api_open": true },
    "mihomo_version": "v1.19.31" }
  ```
- **P0-05 诊断输出**：成功 `{published:true, pool_size, round_id}`；失败
  `{published:false, reason, reject_summary}`。
- **P1-01/02/03 生命周期与历史**：`state/node_history.json` 跨轮保留每节点
  （attempts/success/last_success/last_failure/ip/country/history）；健康状态
  NEW→CHECKING→GOOD/DEGRADED/BAD（最近 5 次窗口）与显式 REMOVED 标记；
  **一次失败不删除历史**（可恢复）。评分 `score = 成功率×50 + 稳定性×30 +
  速度×20` 仅用于排序，**不改变 V5.2 淘汰规则与准入条件**。
- **P1-04 日志分离**：`logs/pool.log`（主流程）、`logs/mihomo.log`（常驻进程）、
  `logs/reject.log`（REJECTED 明细）。

## 生产 Mihomo 常驻（P1-08 细化）

`run --mihomo` 会以最新最终池启动常驻 Mihomo 并在每次刷新发布成功后自动热重载；
也可以独立管理：

```bash
python3 main.py mihomo status    # 进程存活 + 7890/9090 端口探测
python3 main.py mihomo start     # 以最新最终池启动；已在运行则幂等
python3 main.py mihomo reload    # 写新配置 + external-controller 热重载（未运行则写盘回退）
python3 main.py mihomo restart   # 停 → 起
python3 main.py mihomo stop      # 只终止 PID 文件记录的进程（§33）
```

生产常驻架构（P1-08）：Mihomo 负责节点代理/切换/代理连接/出口 IP，
配置生成在 `run/prod/config.yaml`，PID 记录在 `run/prod/mihomo.pid`，
日志在 `logs/mihomo.log`。常驻配置包含：
`external-controller 127.0.0.1:9090`（热重载/健康检查）、
`PROXY`（url-test 自动切换，代理层健康检查走 gstatic generate_204，
不属于 ChatGPT 严格检测）、`GLOBAL`（select 手动兜底）、`rules: MATCH,PROXY`。
检测引擎（18000-18699 逐节点实例）与常驻实例完全独立：目录/端口/日志/PID 互不干扰。

## 已固定的实现规格（P1-06 / P1-07）

| 项 | 默认值（config.json 可改） |
|---|---|
| HOME_ENDPOINT | `https://chat.openai.com/` |
| AUTH_ENDPOINT | `https://chat.openai.com/api/auth/csrf` |
| BACKEND_ENDPOINT | `https://chat.openai.com/backend-api/me` |
| OFFICIAL_COUNTRY_SOURCE_URL | `https://platform.openai.com/docs/supported-countries` |
| GEOIP_URL | `https://ip-api.com/json/` |
| 生产订阅端口 | 18787（仅交付 /chatgpt.yaml） |
| 生产 Mihomo 代理端口 | 7890 |
| 生产 Mihomo API 端口 | 9090（仅 127.0.0.1） |
| 生产检测引擎端口 | 18000-18699 |
| 测试端口 | 18887 + 19000-19999 |
| 刷新周期 | 7200 秒 |

国家代码统一 ISO 3166-1 Alpha-2、内部大写（P1-06）。

## 判定口径要点

- STRICT_PASS = HOME_OK AND (AUTH_OK OR BACKEND_OK)；Backend 401 只算
  BACKEND_AUTH_REQUIRED（网络证据），不参与 BACKEND_OK（P1-01）。
- 单 endpoint 403 不淘汰；HOME/AUTH/BACKEND 全部被明确阻断 → OPENAI_BLOCKED（§13）。
- 稳定性固定 3 次，每次重跑完整 STRICT_PASS，≥2/3 → STABILITY_PASS（P1-02/P1-09）。
- 未达严格通过且无更具体原因 → STRICT_CHECK_FAIL（P1-03），置于 §17 优先级
  STABILITY_FAIL 之后、INTERNAL_ERROR 之前。
- 最终池准入唯一：stage=FINAL AND chatgpt_status=STRICT_PASS AND stability_pass（P1-11）。
- 国家列表三级失败 → COUNTRY_LIST_UNAVAILABLE，中止本轮、保留旧池（§18/P1-12）。
- 发布唯一出口：new_pool 非空 + 验证通过 → 原子替换（P1-13/P1-05）；首次运行失败
  只记 BOOTSTRAP_FAILED，绝不生成空 chatgpt.yaml（§29）。

## 测试

```bash
cd ~/chatgpt-pool-test
python3 run_tests.py        # 全量（场景 A-J，全部 mock，不碰生产）
```
测试隔离：目录、端口、PID、Mihomo 配置/日志全部独立；不 kill 生产进程。

## 验收标准映射（§36）

A 目录分离 ✓ | B 18787 订阅 ✓ | C 18887+19xxx ✓ | D 真实出口 IP 定国家 ✓ |
E 状态机 ✓ | F 严格通过 ✓ | G 稳定性 ≥2/3 ✓ | H 最终池准入 ✓ |
I 旧池保护 ✓ | J 首次运行 BOOTSTRAP_FAILED ✓ | K 测试隔离 ✓
