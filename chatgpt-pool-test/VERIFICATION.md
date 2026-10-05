# 验证报告（2026-10-05）

实现：ChatGPT 免费公共节点池 Termux（规则 V5.2 + 补丁附录 V5.2-P1）
容器环境：Python 3.12.11，无 mihomo（Termux 安装），有外网（部分源可达）。

## 1. 单元/Mock 全量测试：42/42 通过

| 文件 | 验证内容 |
|---|---|
| test_parser.py | ss/vmess/vless/trojan/http/socks 解析；node_id 稳定（改名不变、改凭据变）；去重 |
| test_country.py | 三级回退（在线→缓存→内置）；缓存损坏回退；数量不足拒绝；COUNTRY_LIST_UNAVAILABLE |
| test_chatgpt.py | STRICT_PASS；Backend 401 仅 NETWORK_PASS（P1-01）；单 403 不淘汰；全阻断 OPENAI_BLOCKED；TLS/超时映射；页面特征必需 |
| test_stability.py | 3/3、2/3 通过；1/3、0/3 拒绝；固定 3 次（P1-09，多跑即报错） |
| test_priority.py | §17 优先级顺序；STRICT_CHECK_FAIL 位于 STABILITY_FAIL 后、INTERNAL_ERROR 前 |
| test_pool_guard.py | 国家列表不可用保旧池；首次运行 BOOTSTRAP_FAILED 不生成空 YAML；0 合格节点保旧池；原子替换无残留；REJECTED+失败原因保留 |
| test_server.py | /chatgpt.yaml 交付、404、生产端口 18787 未被占用、测试端口 18887 可用 |
| test_pipeline_e2e.py | 场景 A 端到端：获取→解析→去重→FINAL→发布，raw/normalized/results/final/state 齐全 |

## 2. 真实网络验证

| 项 | 结果 |
|---|---|
| 4 个公共节点源抓取 | ✅ 全部 200；解析 842 节点、无效行 2906、去重后 769（ss155/trojan80/vmess64/vless287/https182/socks5 1） |
| 国家在线列表 | ⚠️ platform.openai.com 当前不可达 → 按 §6 回退到内置列表 + COUNTRY_LIST_FALLBACK（行为符合规则；Termux 上可改用可访问的来源 URL 并等缓存生成） |
| 订阅服务冒烟 | ✅ 127.0.0.1:18887/chatgpt.yaml 返回正确内容；/other→404；/healthz→ok；check 命令正常 |
| 端口隔离 | ✅ 测试只占 18887/19xxx；18787/7890/18000-18699 全程未被测试触碰 |

## 3. 沙箱无法验证、需在 Termux 上验证的项

- mihomo 真实启动/停止与 PID 隔离（引擎层用 FakeEngine 覆盖了逻辑，真实进程需 `pkg install mihomo`）。
- 真实出口 IP/GeoIP 经代理链路。
- 真实 ChatGPT 检测（HOME/AUTH/BACKEND 端点可能随 OpenAI 调整，config.json 可改）。
- 定时刷新长跑（`python3 main.py run` 2 小时周期）。

## 4. 修复记录（实现过程中）

1. parser：vmess://、vless:// 前缀为 8 字符，修正切片偏移（曾多留 `/` 导致解析失败）。
2. helpers：测试 base 目录先创建再 mkdtemp。
3. fetchers：注入来源支持字面量文本（测试便利，生产不受影响）。
4. yaml_lite：订阅条目缩进统一为 Clash 兼容的 2/4 空格，校验改为 strip 后匹配（原 4 空格缩进导致校验误判）。
5. 默认节点源：实测替换 2 个 404 源（aiboboxx、monosans → ermaozi、TheSpeedX），保留 mahdibland、freefq。
