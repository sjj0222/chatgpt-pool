# 验证报告（2026-10-06，含 V0.2.1 production hardening）

实现：ChatGPT 免费公共节点池 Termux（规则 V5.2 + V5.2-P1 + V0.2.1 hardening）
容器环境：Python 3.12.11，无真实 mihomo 二进制（Termux 安装），有外网（部分源可达）。

## 1. 单元/Mock 全量测试：109/109 通过（78 基线 + 31 新增）

| 文件 | 验证内容 |
|---|---|
| test_parser.py | ss/vmess/vless/trojan/http/socks 解析；node_id 稳定；去重 |
| test_country.py | 三级回退；缓存损坏回退；数量不足拒绝；COUNTRY_LIST_UNAVAILABLE |
| test_chatgpt.py | STRICT_PASS；Backend 401 仅网络证据；单 403 不淘汰；全阻断 OPENAI_BLOCKED |
| test_stability.py | 3/3、2/3 通过；1/3、0/3 拒绝；固定 3 次 |
| test_priority.py | §17 优先级；STRICT_CHECK_FAIL 位置 |
| test_pool_guard.py | 国家列表不可用保旧池；BOOTSTRAP_FAILED；0 合格节点保旧池；原子替换 |
| test_server.py | /chatgpt.yaml 交付、404、生产 18787 未占用、18887 可用 |
| test_pipeline_e2e.py | 场景 A 端到端发布；final/current.json 结构（P0-02） |
| test_mihomo_svc.py | 常驻 start/stop/status/reload；PID 隔离；NO_ACTIVE_POOL（P0-03） |
| test_hardening.py | V0.2.1：reject_summary 真实统计；final 隔离；start 保护；status 增强；失败恢复；节点历史；reject.log |
| test_diagnostic.py（新增 31） | V0.2.2：14 细分原因映射（TCP_TIMEOUT/REFUSED/ERROR、PROXY_HANDSHAKE_FAIL、TLS_TIMEOUT/CERT/SNI/HANDSHAKE、NO_EGRESS_IP、EGRESS_TIMEOUT、CHATGPT_BLOCKED/TIMEOUT、CONFIG_INVALID、UNKNOWN_FAIL）；egress/chatgpt 细分；failed/nodes jsonl 字段完整 + 1000 上限 + 无 REJECTED 不写；reject_summary_detailed / by_protocol / stage_summary；顶层 §16 保留；source_health（fetch=false、alive_rate、pipeline 写入）；detect.log；节点诊断三要素 + FINAL 干净；status last_refresh/last_failure/current_pool_age；refresh_state 成功/失败 |

## 2. 真实网络验证

| 项 | 结果 |
|---|---|
| 4 个公共节点源抓取 | ✅ 全部 200；解析 842 节点、无效行 2906、去重后 769（ss155/trojan80/vmess64/vless287/https182/socks5 1） |
| 国家在线列表 | ⚠️ platform.openai.com 当前不可达 → 按 §6 回退内置列表 + COUNTRY_LIST_FALLBACK |
| 订阅服务冒烟 | ✅ 127.0.0.1:18887/chatgpt.yaml、404、healthz、check 正常 |
| 端口隔离 | ✅ 测试只占 18887/19xxx；生产端口全程未被触碰 |

## 3. V0.2.1 真实完整刷新冒烟（沙箱，无 mihomo）

真实抓取 4 源（60 节点上限）+ 完整 pipeline，验证失败路径与诊断：

| 项 | 结果 |
|---|---|
| refresh 摘要 | ✅ published:false / reason:EMPTY_POOL / total:60 / invalid:2525 / dupes:100 / bootstrap_failed:true（首次无池） |
| reject_summary | ✅ {"PROXY_START_FAIL": 60}（来自真实检测；沙箱无 mihomo → 引擎构造失败按节点级 PROXY_START_FAIL 处理，不再拖垮整轮） |
| final 隔离 | ✅ 无 current.json 生成（未污染）；final/failed/20261007000021.json 存在且含诊断 |
| 空池保护 | ✅ chatgpt.yaml 未生成（首次失败不产出空订阅） |
| node_history | ✅ state/node_history.json 记录 60 节点 |

## 4. 生产 Mihomo 常驻：真实进程级冒烟（fake 二进制）

| 步骤 | 结果 |
|---|---|
| start | ✅ 双端口（7890/9090）就绪判定；PID 写入；重复 start 幂等 |
| status | ✅ running/active_pool/ports/version 全部正确 |
| reload（fake 无真实 API） | ✅ API 失败回退：配置写盘、运行实例不受影响 |
| stop | ✅ 只终止 PID 文件进程；wait 回收（无僵尸）；PID 文件清理 |

## 5. 沙箱无法验证、需在 Termux 上验证的项

- P0-06 六步真实测试（mihomo -v / init / refresh / start / status / stop）：
  已提供 `chatgpt-pool/test_termux.sh`，在 Termux 上 `bash test_termux.sh` 执行。
- 真实 mihomo 的启动/热重载 API 行为、真实出口 IP/GeoIP、真实 ChatGPT 检测
  （HOME/AUTH/BACKEND 端点可能随 OpenAI 调整，config.json 可改）、定时刷新长跑。

## 6. 修复记录

1. parser：vmess://、vless:// 前缀 8 字符切片修正。
2. helpers：测试 base 目录先创建。
3. fetchers：注入来源支持字面量文本。
4. yaml_lite：订阅缩进统一 2/4 空格；校验 strip 匹配。
5. 默认节点源：实测替换 2 个 404 源（aiboboxx、monosans → ermaozi、TheSpeedX）。
6. mihomo_svc：stop wait 回收子进程防僵尸。
7. pipeline（V0.2.1 真实冒烟暴露）：引擎构造失败（如二进制缺失）原先在 try 外抛
   异常导致整轮 refresh 崩溃 → 改为节点级 PROXY_START_FAIL 拒绝，不拖垮整轮；
   失败日志由整段 traceback 收敛为单行 warning。

## 7. V0.2.2 真实完整刷新冒烟（沙箱，无 mihomo）

真实抓取 4 源（60 节点上限）+ 完整 pipeline，验证诊断链路（失败路径）：

| 项 | 结果 |
|---|---|
| 顶层 reject_summary | ✅ {"PROXY_START_FAIL": 60}（§16 权威枚举不变） |
| 细分 reject_summary_detailed | ✅ {"PROXY_HANDSHAKE_FAIL": 60}（引擎构造失败→代理握手细分） |
| stage_summary | ✅ {"PROXY": 60}（失败阶段可定位） |
| reject_summary_by_protocol | ✅ ss 57 / trojan 2 / vmess 1，全部 failed + 细分（协议整体失效可判定） |
| failed/nodes jsonl | ✅ final/failed/nodes/{round}.jsonl 60 行，字段完整（node_id/protocol/server/port/source/stage/failure_reason/failure_stage/failure_detail/sub_reason） |
| source_health.json | ✅ 真实源 fetch/parsed/valid/alive/alive_rate；含 fetch=true 但 parsed=0 的源（源可用≠内容可解析） |
| detect.log | ✅ "node_id PROXY START/END FAIL PROXY_START_FAIL" 逐阶段记录 |
| refresh_state | ✅ last_refresh / last_failure_reason=EMPTY_POOL / published=false |
| 空池保护 | ✅ current.json 未生成、chatgpt.yaml 未生成（首次失败不产出空订阅） |

## 8. V0.2.2 修复记录

1. test_diagnostic 测试数据：NODE_A 末尾 `#nodeA` 缺换行导致 trojan 行被吞成注释
   （解析后仅 2 节点）→ 补换行。
2. egress：`if not ip` 分支 detail 补 body_snippet（与 JSON 解析失败分支一致）。
3. poolstore.read_current_final：返回值增加 published_at（供 status 计算
   current_pool_age）。
4. pipeline：`(str, Enum)` 的 str() 返回 `SubReason.TLS_TIMEOUT` 而非值 →
   stage 派生改用 `.value` 比较；chatgpt 检测内传输层失败（TCP_*/TLS_*）阶段
   归 TLS（贴合 V0.2.2 示例 TLS_TIMEOUT→TLS），业务性失败归 CHATGPT。

## 9. V0.2.2.1 Diagnostic Fix 验证

修复 Termux 真机验收问题：诊断产物路径统一 run/ + test_termux.sh shell 兼容。
核心逻辑（准入/淘汰/评分/顶层 failure_reason/pipeline/空池保护/mihomo 启停）
未改动。

| 项 | 结果 |
|---|---|
| 单元/Mock 全量 | ✅ 109/109 全绿（V0.2.2 基线保持；refresh_state 断言强化 round_id/status/pool_size；detect.log 断言强化轮次摘要五要素） |
| bash -n test_termux.sh | ✅ 无输出、exit 0（原 `*"started":\ true*` 非法 glob → 单引号 `*'"started": true'*`） |
| check_file 验收 | ✅ 新增 check_file run/source_health.json / run/detect.log / run/refresh_state.json（PASS/FAIL 文案） |
| CLI 真实冒烟（沙箱，真实 4 源 300 节点） | ✅ run/failed/nodes/{round}.jsonl 300 行、run/source_health.json、run/detect.log（含 round=/Detection Result/Stage/Reason/Protocol 摘要块）、run/refresh_state.json（round_id/status/published/pool_size）全部落盘；EMPTY_POOL 不生成 current.json 与 chatgpt.yaml（空池保护不变） |
| 路径迁移范围 | ✅ poolstore（current/failed/nodes/refresh_state）、pipeline（source_health + detect 摘要）、main（detect handler→run/）；测试断言同步迁移，无旧路径残留 |

## 10. V0.2.2.1 修复记录

1. detect.log 摘要：V0.2.2 只有逐节点 START/END 事件 → V0.2.2.1 在 _finish /
   _finish_failed 追加轮次摘要（round_id / Detection Result / Stage / Reason /
   Protocol），满足 P0-01"detect.log 至少包含五项"。
2. refresh_state：补齐 round_id / status 字段（满足 P0-01 四字段要求），
   保留 last_refresh/last_success/last_failure_reason 供 status 读取。
3. main._setup_logging：为 run/detect.log 预建 run/ 目录（mihomo 子命令
   不经 ensure_dirs 也能落日志）。
4. 测试断言：`assertFalse(os.path.isdir(run/failed))` 改为断言
   run/failed 无 .json 摘要且 run/failed/nodes 为空（ensure_dirs 预建目录，
   目录存在不代表写入失败轮次）。
5. run_tests 新增/强化断言（refresh_state 四字段、detect.log 五要素），
   总测试数维持 109 全绿。

## 11. V0.2.2.2 Diagnostic Persistence + Lifecycle Cleanup 验证

修复失败路径诊断产物缺失（系统级提前 return 时 source_health/failed-nodes 不落盘）
与 mihomo stop 端口残留误报。核心逻辑零改动（准入/淘汰/评分/§16/SubReason/
pipeline/空池保护/原子替换/mihomo start 与 config）。

| 项 | 结果 |
|---|---|
| 单元/Mock 全量 | ✅ 118/118 全绿（109 基线 + 9 新增：Persistence 5 + Lifecycle 4） |
| Persistence | ✅ 空池写盘（failed round + failed/nodes 明细 + source_health + detect.log + refresh_state 五产物）；bootstrap_failed 写盘；COUNTRY_LIST_UNAVAILABLE 提前 return 兜底（fetch/parsed 真实、valid=0/alive=0、failed/nodes 空文件、detect.log Reason=原因）；INTERNAL_ERROR 全部源失败（每源 fetch=false/parsed=0、failed/nodes 空文件）；refresh_state 六字段两态 |
| Lifecycle | ✅ stop 等待/确认 PID 退出/确认 7890+9090 释放；pid 不存在（pid_exited=True、端口视为释放）；端口未释放（ports={7890:False}、绝不 kill 其他进程）；CLI 输出 PASS port released / PORT_RELEASE_TIMEOUT port=7890 |
| 真实冒烟（COUNTRY_LIST_UNAVAILABLE） | ✅ 五产物全落盘：failed/round.json、failed/nodes（空文件 0B）、source_health（SOURCE_A fetch=true parsed=3 valid=0 alive=0）、detect.log（round=/Stage:(none)/Reason:COUNTRY_LIST_UNAVAILABLE/Protocol:(none)/Detection Result）、refresh_state（status=COUNTRY_LIST_UNAVAILABLE + 六字段）；bootstrap_failed=True、无空 chatgpt.yaml |
| bash -n test_termux.sh | ✅ 无输出、exit 0；step 6 Lifecycle 验收文案更新 |

## 12. V0.2.2.2 修复记录

1. detect.log 摘要改为直接落盘 run/detect.log（不依赖 main 的 logger handler），
   保证 pipeline 直调/提前 return 路径产物必在；五要素无数据时输出 (none)。
2. _finish_failed 增加 raw_items/parsed_nodes/nodes 参数，系统级失败兜底收集
   source_health（fetch/parsed 真实、valid=0——节点未进入检测流程）并
   write_failed_nodes(always=True) 生成空 jsonl。
3. write_failed_nodes 增加 always 参数（无 REJECTED 也创建空文件）；
   _finish 未发布分支补 failed/nodes（VALIDATION_FAIL 无 REJECTED 场景），
   已存在则不重复覆盖（EMPTY_POOL 由步骤 4 写入）。
4. mihomo stop：kill 后 _wait_ports_released 轮询（1 秒一次、最多 5 秒）
   确认 7890/9090 释放；返回 pid_exited/ports；CLI 打印 PASS/
   PORT_RELEASE_TIMEOUT。只操作 PID 文件记录的进程（§33）。

## 13. V0.3 Phase 1 数据模型扩展 验证

V0.3 按 Phase 1-10 严格推进（用户每阶段确认后进入下一阶段）。Phase 1 纯增量：
新增枚举/结构/存储，不改动 V0.2.2.2 任何既有枚举、格式与行为。

| 项 | 结果 |
|---|---|
| 单元/Mock 全量 | ✅ 141/141 全绿（118 基线 + 23 新增 test_models_v03.py） |
| 生命周期枚举 | ✅ 11 状态完整（DISCOVERED…BLACKLIST）；源 3 状态；能力 5 标签；事件 7 类 |
| 评分结构 | ✅ 权重 40/30/20/10；compute_overall 复算一致；breakdown 可展示；service_score 多维 |
| 指纹 | ✅ 同参稳定；凭据/地址变化区分；名称/来源变化不变（迁移识别）；参数顺序无关；无明文 |
| 向后兼容 | ✅ Node 新字段默认 ""；旧 dict（无 node_fingerprint）可读 |
| schema 版本 | ✅ 新文件写 SCHEMA_VERSION=3；旧文件无版本兼容读；upsert 保留未知字段（只增不改） |
| 事件 | ✅ 追加记录、保留最近 50 条 |
| 未改动 | ✅ failure_reason/DiagnosticStage/SubReason/Stage/ChatgptStatus/pipeline/诊断产物格式全部保持 |
