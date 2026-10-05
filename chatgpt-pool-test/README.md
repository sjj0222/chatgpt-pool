# chatgpt-pool-test（V5.2 §3 / §32 / §33 / §34 / §36K）

测试与生产完全隔离：

| 项 | 测试 | 生产 |
|---|---|---|
| 目录 | `~/chatgpt-pool-test/`（及本目录 base/ 临时子目录） | `~/chatgpt-pool/` |
| 订阅端口 | 18887 | 18787 |
| 动态端口 | 19000-19999 | 18000-18699（检测）/ 7890（Mihomo） |
| Mihomo | FakeEngine（不启动进程、无 PID） | 真实 mihomo，独立 PID/配置/日志 |
| 数据 | 只写本目录 | raw/normalized/results/final |

测试不访问任何真实网络：节点源、GeoIP、ChatGPT 端点、国家在线列表全部 Mock。

## 运行

```bash
cd ~/chatgpt-pool-test
python3 run_tests.py        # 或 python3 -m unittest discover -s . -p 'test_*.py'
```

## 场景覆盖（§35）

| 测试文件 | 覆盖场景 |
|---|---|
| test_parser.py | 解析（ss/vmess/vless/trojan/http/socks）、node_id 去重、RAW 保留 |
| test_country.py | 场景 G/H：在线→缓存→内置三级回退、COUNTRY_LIST_UNAVAILABLE |
| test_chatgpt.py | 场景 A/D/E/F：STRICT_PASS、单 403 不淘汰、全阻断 OPENAI_BLOCKED、Backend 401（P1-01）、TLS/DNS/超时映射、页面特征 |
| test_stability.py | 场景 B/C：3/3、2/3 通过；1/3、0/3 拒绝；固定 3 次（P1-09） |
| test_priority.py | §17 失败优先级（含 P1-03 位置） |
| test_pool_guard.py | 场景 H/I/J：国家列表不可用保旧池、首次运行 BOOTSTRAP_FAILED 不生成空 YAML、0 合格节点保旧池、原子替换、REJECTED 结果保留 |
| test_server.py | §25/P1-08：订阅服务交付、404、生产端口 18787 未被占用、18887 可用 |
| test_pipeline_e2e.py | 场景 A 端到端：获取→解析→去重→FINAL→发布，四类产物齐全 |
