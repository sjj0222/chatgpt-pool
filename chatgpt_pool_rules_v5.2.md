# ChatGPT 免费公共节点池 Termux 开发规则 V5.2（终版存档）

> 存档日期：2026-10-05 ｜ 本文件为执行基准。其他 AI 执行本项目时，以本文件为准，不得自行修改规则条目；发现规则冲突时先停止实现并报告，不得自行选择一个版本覆盖另一个版本。

---

ChatGPT 免费公共节点池 Termux 开发规则 V5.2

1. 项目目标

在 Android + Termux 环境下，建立一个本地运行的 ChatGPT 免费公共代理节点池。

核心目标：

1. 自动获取免费公共代理节点。


2. 解析、清洗、去重。


3. 通过代理获取真实出口 IP。


4. 根据真实出口国家筛选支持国家。


5. 对节点进行 OpenAI / ChatGPT 可达性检测。


6. 进行稳定性复测。


7. 只有达到严格条件的节点才能进入最终池。


8. 定时自动刷新。


9. 刷新失败时保护旧池，绝不能因为一次刷新失败把可用旧池清空。


10. 整个系统在 Termux 本地运行。


11. 不依赖 ChatGPT 登录、Cookie 或用户账号。




---

2. 运行环境

生产环境

Android
└── Termux
    └── ~/chatgpt-pool/

生产目录固定：

~/chatgpt-pool

生产服务端口：

18787

最终订阅地址：

http://127.0.0.1:18787/chatgpt.yaml


---

3. 测试环境

测试必须与生产环境完全隔离。

测试目录：

~/chatgpt-pool-test

测试端口：

18887

以及测试过程中需要使用的动态端口：

19000-19999

要求：

测试不能覆盖生产配置。

测试不能删除生产池。

测试不能修改生产运行中的 Mihomo。

测试 PID 必须与生产 PID 隔离。

测试代理端口必须与生产端口隔离。

测试生成的数据必须写入测试目录。

Mock 数据只能进入测试环境。

不允许为了测试直接修改生产节点池。



---

4. 核心原则

4.1 不相信节点名称

以下信息不得作为节点国家判定依据：

节点名称

节点备注

域名

SNI

URL 中的国家代码

节点标签

机场名称


例如：

美国节点
US
United States
Los Angeles
美国IP

这些都不能直接证明节点实际位于美国。


---

5. 国家判定

国家判定必须遵循：

节点
↓
代理启动
↓
通过代理访问 IP/GeoIP 服务
↓
获得真实出口 IP
↓
IP → ISO Alpha-2
↓
SUPPORTED_COUNTRIES
↓
国家通过/拒绝

即：

> 只认真实出口 IP 所对应的国家。



不得使用节点名称推断国家。


---

6. 支持国家列表

国家列表采用三级来源机制。

优先级：

① 官方在线列表
        ↓失败
② 本地缓存列表
        ↓失败
③ 内置最近确认列表

本地缓存：

~/chatgpt-pool/supported_countries.json

① 官方在线列表

优先获取最新支持国家列表。

② 本地缓存

官方在线获取失败时，读取：

~/chatgpt-pool/supported_countries.json

③ 内置列表

在线和缓存都失败时，使用程序内置的最近确认列表，同时记录：

COUNTRY_LIST_FALLBACK

并产生警告。

三者全部失败

如果：

官方在线失败
AND
本地缓存不存在/不可读
AND
内置列表不可用

则：

COUNTRY_LIST_UNAVAILABLE

此时：

禁止继续进行节点国家筛选并生成新池。

必须：

中止本轮刷新
↓
保留旧池

如果系统第一次运行、根本没有旧池：

BOOTSTRAP_FAILED

并且：

不得生成空的 chatgpt.yaml。


---

7. 节点生命周期

统一使用以下状态：

NEW
 ↓
PARSED
 ↓
EGRESS_OK
 ↓
COUNTRY_OK
 ↓
CHATGPT_CHECKED
 ↓
STABILITY_CHECKED
 ↓
FINAL

任何节点在中途无法满足要求：

→ REJECTED

因此完整状态集合：

NEW
PARSED
EGRESS_OK
COUNTRY_OK
CHATGPT_CHECKED
STABILITY_CHECKED
FINAL
REJECTED


---

8. 状态含义

NEW

节点刚进入系统，尚未解析。

PARSED

节点格式合法，已经成功解析。

EGRESS_OK

代理能够正常启动，并成功取得真实出口 IP。

COUNTRY_OK

真实出口国家存在，并且属于支持国家列表。

CHATGPT_CHECKED

已经完成 ChatGPT/OpenAI 核心检测。

注意：

> CHATGPT_CHECKED 不代表一定通过。



检测结果由独立的：

chatgpt_status

记录。


---

9. chatGPT 状态

chatgpt_status 与 stage_status 分开。

核心状态至少包括：

UNKNOWN
NETWORK_PASS
STRICT_PASS
FAIL

这样避免把：

节点完成检测

错误理解成：

节点一定可用


---

10. 严格通过条件

最终严格通过：

STRICT_PASS =
HOME_OK
AND
(AUTH_OK OR BACKEND_OK)

也就是说：

HOME

ChatGPT/OpenAI Home 页面检测必须通过：

HOME_OK

AUTH

认证相关 endpoint 通过：

AUTH_OK

或者：

BACKEND

Backend endpoint 提供有效网络可达证据：

BACKEND_OK

最终：

HOME_OK
AND
(AUTH_OK OR BACKEND_OK)

才能：

STRICT_PASS


---

11. HTTP 检测不能只看 200

对于 HOME / AUTH 等 HTTP 检测，不能只写：

HTTP 200 = 成功

必须综合判断：

HTTP 状态码

最终 URL

Content-Type

页面特征

请求是否经过目标代理

是否存在明显错误页面


允许：

2xx
3xx

但必须经过最终 URL 和页面特征确认。


---

12. Backend 401 的特殊处理

如果 Backend 返回：

401

不得简单判定：

节点不可用

应记录：

BACKEND_AUTH_REQUIRED

它可以作为：

NETWORK_PASS

的证据。

但是：

> BACKEND_AUTH_REQUIRED 不能单独替代 HOME/AUTH 的严格通过条件。




---

13. 403 的处理

单个 endpoint：

403

不能直接淘汰节点。

例如：

HOME = 403
AUTH = 200
BACKEND = 401

不能因为 HOME 403 就立即判死刑。

只有核心检测全部被明确阻断，例如：

HOME = 403
AUTH = 403
BACKEND = 403

才判：

OPENAI_BLOCKED


---

14. WebSocket

WebSocket 检测属于：

辅助检测

不是最终否决条件。

因此：

WS_FAIL

不能单独导致节点：

REJECTED

除非未来版本明确改变规则。


---

15. 网络检测原则

必须区分：

代理启动失败

和：

目标网站不可达

以及：

OpenAI 主动拒绝

不能全部归类成：

节点失效

必须记录具体失败原因。


---

16. 失败原因统一枚举

统一使用：

INVALID_NODE
PROXY_START_FAIL
NO_EGRESS_IP
COUNTRY_LIST_UNAVAILABLE
UNSUPPORTED_COUNTRY
OPENAI_BLOCKED
TLS_FAIL
DNS_FAIL
PROXY_ERROR
TIMEOUT
CHATGPT_UNREACHABLE
STABILITY_FAIL
INTERNAL_ERROR


---

17. 失败优先级

如果一次检测出现多个问题，统一按照以下优先级确定最终 failure_reason：

INVALID_NODE
>
PROXY_START_FAIL
>
NO_EGRESS_IP
>
COUNTRY_LIST_UNAVAILABLE
>
UNSUPPORTED_COUNTRY
>
OPENAI_BLOCKED
>
TLS_FAIL
>
DNS_FAIL
>
PROXY_ERROR
>
TIMEOUT
>
CHATGPT_UNREACHABLE
>
STABILITY_FAIL
>
INTERNAL_ERROR

这样可以避免同一个节点在不同运行中出现不同的最终失败原因。


---

18. 国家失败与节点失败必须区分

系统级失败

例如：

COUNTRY_LIST_UNAVAILABLE

属于：

> 本轮刷新无法可靠执行。



因此：

中止刷新
保留旧池

不是把所有节点标记成：

UNSUPPORTED_COUNTRY

节点级失败

例如：

UNSUPPORTED_COUNTRY

表示：

> 国家列表正常，但这个节点自己的出口国家不符合要求。



因此可以正常淘汰这个节点，继续检测其他节点。

这是两个完全不同的逻辑。


---

19. 最终池进入条件

一个节点进入最终池必须同时满足：

stage_status = FINAL
AND
chatgpt_status = STRICT_PASS
AND
稳定性检测成功 ≥ 2/3

即：

> STRICT_PASS + 稳定性至少 2/3，才允许进入最终池。



单纯：

NETWORK_PASS

不能进入最终池。

单次偶然成功也不能直接进入最终池。


---

20. 稳定性检测

通过首次严格检测后进行稳定性复测。

至少：

3 次

最终要求：

成功次数 >= 2

即：

3/3 → 通过
2/3 → 通过
1/3 → 拒绝
0/3 → 拒绝

失败：

STABILITY_FAIL


---

21. 节点淘汰原则

节点出现确定性失败时：

stage_status = REJECTED

同时记录：

failure_reason

不能出现：

stage_status = FINAL
failure_reason = xxx

这种自相矛盾状态。

也不能出现：

REJECTED
但仍被写入最终池


---

22. FINAL 是唯一最终池准入状态

最终池只接受：

stage_status = FINAL

并且：

chatgpt_status = STRICT_PASS
stability_pass = true

任何其他状态：

NEW
PARSED
EGRESS_OK
COUNTRY_OK
CHATGPT_CHECKED
STABILITY_CHECKED
REJECTED

都不能直接进入最终池。


---

23. 节点去重

节点进入检测流程前必须去重。

至少考虑：

协议
服务器
端口
账号
密码
UUID
网络 ID

具体按照节点协议生成稳定的：

node_id

不能仅根据：

节点名称

去重。


---

24. RAW 与推理结果分离

原始节点数据与检测结果必须分离。

原则：

RAW
≠
检测结果
≠
最终池

RAW 数据不得因为检测结果而被覆盖。

推荐逻辑：

raw/
normalized/
results/
final/

即使节点被淘汰，也保留检测结果和失败原因。


---

25. 最终订阅池

生产最终输出：

~/chatgpt-pool/chatgpt.yaml

通过：

127.0.0.1:18787

提供：

/chatgpt.yaml


---

26. 刷新周期

默认：

7200 秒

即：

2 小时

自动执行一次刷新。

流程：

获取节点
↓
解析
↓
去重
↓
启动代理
↓
真实出口 IP
↓
国家判定
↓
ChatGPT/OpenAI 检测
↓
稳定性检测
↓
淘汰失败节点
↓
生成候选最终池
↓
验证
↓
原子替换生产池


---

27. 旧池保护

这是 V5.2 的核心规则之一。

任何刷新操作都不能直接：

rm chatgpt.yaml

然后再生成新文件。

必须：

生成新池临时文件
↓
完整验证
↓
验证成功
↓
原子替换旧池

例如：

chatgpt.yaml.tmp

验证通过后：

chatgpt.yaml.tmp
→
chatgpt.yaml


---

28. 刷新失败时

如果本轮刷新失败：

不得覆盖旧池

继续使用上一版本：

chatgpt.yaml

例如：

旧池 20 个节点
↓
本轮刷新失败
↓
旧池仍然保持 20 个节点

而不是：

刷新失败
↓
chatgpt.yaml = 空


---

29. 第一次运行

如果：

旧池不存在

并且本轮刷新成功：

正常生成首次池

如果本轮刷新失败：

BOOTSTRAP_FAILED

必须：

不生成空 YAML

即：

> 第一次运行失败 ≠ 创建一个空节点池。




---

30. 严禁“空池覆盖旧池”

即使本轮：

最终有效节点 = 0

也不能直接覆盖已有旧池。

必须根据刷新结果判断：

是正常检测后确实没有合格节点

还是：

系统级检测失败

系统级失败：

保留旧池


---

31. 系统级失败与节点级失败

节点级失败

例如：

INVALID_NODE
UNSUPPORTED_COUNTRY
OPENAI_BLOCKED
TLS_FAIL
DNS_FAIL
TIMEOUT
STABILITY_FAIL

可以淘汰单个节点。

系统级失败

例如：

COUNTRY_LIST_UNAVAILABLE

以及导致整个刷新流程无法可靠完成的内部故障：

INTERNAL_ERROR

应停止本轮发布并保护旧池。


---

32. 生产与测试命令原则

所有命令必须明确工作目录。

生产：

cd ~/chatgpt-pool

测试：

cd ~/chatgpt-pool-test

不得：

在测试目录运行生产启动命令

也不得：

在生产目录执行 Mock 测试


---

33. Mihomo 隔离

生产 Mihomo 与测试 Mihomo：

PID 独立
配置独立
端口独立
日志独立
运行目录独立

测试不能：

kill 生产 Mihomo

生产也不能读取测试配置。


---

34. Mock 测试

必须提供 Mock 测试能力，用于验证：

节点解析

国家判定

国家列表失败

状态机

ChatGPT 检测结果

403

401

TLS

DNS

Timeout

稳定性

淘汰

旧池保护

首次运行

原子替换


Mock 测试不能污染真实生产池。


---

35. 必须测试的关键场景

场景 A：正常成功

节点解析成功
→
出口 IP 成功
→
国家支持
→
HOME_OK
→
AUTH_OK/BACKEND_OK
→
STRICT_PASS
→
稳定性 3/3
→
FINAL

必须进入最终池。


---

场景 B：稳定性 2/3

STRICT_PASS
+
稳定性 2/3

必须进入最终池。


---

场景 C：稳定性 1/3

必须：

REJECTED
STABILITY_FAIL

不得进入最终池。


---

场景 D：单个 endpoint 403

不能直接：

REJECTED

必须继续综合判断。


---

场景 E：核心 endpoint 全部 403

OPENAI_BLOCKED

节点拒绝。


---

场景 F：Backend 401

记录：

BACKEND_AUTH_REQUIRED

可以作为网络可达证据，但不能单独产生：

STRICT_PASS


---

场景 G：国家列表在线失败

如果缓存存在：

使用缓存

如果缓存也没有：

使用内置列表
+
COUNTRY_LIST_FALLBACK


---

场景 H：国家列表全部不可用

COUNTRY_LIST_UNAVAILABLE

本轮刷新：

中止

旧池：

保留


---

场景 I：首次运行 + 国家列表不可用

BOOTSTRAP_FAILED

不得生成：

空 chatgpt.yaml


---

场景 J：本轮所有节点都失败

如果旧池存在：

旧池保留

不能因为本轮结果为空就把旧池清空。


---

36. 可执行验收标准

V5.2 不能只做到：

代码能运行

必须满足以下验收条件。

A. 目录

~/chatgpt-pool
~/chatgpt-pool-test

生产和测试分离。

B. 生产端口

18787

可以正常访问：

/chatgpt.yaml

C. 测试端口

18887
19000-19999

不能与生产冲突。

D. 国家判定

必须由：

真实出口 IP

确定国家。

节点名称不能直接决定国家。

E. 状态机

必须严格遵循：

NEW
→ PARSED
→ EGRESS_OK
→ COUNTRY_OK
→ CHATGPT_CHECKED
→ STABILITY_CHECKED
→ FINAL

失败：

→ REJECTED

F. 严格通过

必须满足：

HOME_OK
AND
(AUTH_OK OR BACKEND_OK)

G. 稳定性

必须：

≥2/3

H. 最终池

只能接受：

FINAL
+
STRICT_PASS
+
STABILITY_PASS

I. 旧池

刷新失败：

旧池不能丢失

J. 首次运行

首次刷新失败：

不得产生空 YAML

必须：

BOOTSTRAP_FAILED

K. 测试隔离

Mock 测试：

不能影响生产池
不能杀生产进程
不能修改生产配置


---

37. 开发纪律

其他 AI 执行本规则时：

1. 不得自行增加产品功能。


2. 不得擅自改变 V5.2 的状态名称。


3. 不得擅自改变最终准入条件。


4. 不得用节点名称代替真实出口国家。


5. 不得用一次 HTTP 成功代替严格 ChatGPT 检测。


6. 不得把 NETWORK_PASS 当成 STRICT_PASS。


7. 不得把单个 403 直接判死刑。


8. 不得把 Backend 401 当作严格通过。


9. 不得因为刷新失败清空旧池。


10. 不得让测试环境污染生产环境。


11. 修改代码后必须进行对应验收测试。


12. 发现规则冲突时，先停止实现并报告冲突，不得自行选择一个版本覆盖另一个版本。




---

38. V5.2 最终判定模型

整个系统最终可以压缩成这一条：

公共节点
  ↓
PARSED
  ↓
代理可启动？
  ├─ 否 → REJECTED / PROXY_START_FAIL
  ↓
真实出口 IP？
  ├─ 否 → REJECTED / NO_EGRESS_IP
  ↓
国家列表可用？
  ├─ 否 → 本轮刷新失败 / 保留旧池
  ↓
真实出口国家支持？
  ├─ 否 → REJECTED / UNSUPPORTED_COUNTRY
  ↓
ChatGPT/OpenAI 核心检测
  ↓
HOME_OK
AND
(AUTH_OK OR BACKEND_OK)
  ↓
STRICT_PASS
  ↓
稳定性 3 次
  ↓
≥2 次成功？
  ├─ 否 → REJECTED / STABILITY_FAIL
  ↓
FINAL
  ↓
写入临时池
  ↓
完整验证
  ↓
原子替换生产池

---

ChatGPT 免费公共节点池 Termux 开发规则 V5.2

补丁附录 V5.2-P1

性质：权威口径补丁

本附录不改变 V5.2 的总体判定模型，仅对 V5.2 中发现的歧义、枚举缺口及实现规格缺口进行明确。

如本附录与 V5.2 正文存在表述差异：

> 以本附录为最终执行口径。




---

P1-01：BACKEND 401 不得参与 STRICT_PASS

对应原规则：

§10

§11

§12


权威规则

Backend 返回：

401 Unauthorized

只能表示：

BACKEND_AUTH_REQUIRED

并可作为：

NETWORK_PASS

的网络可达性证据。

401 不得计入 BACKEND_OK。

因此：

BACKEND_401
≠
BACKEND_OK

并且：

HOME_OK + BACKEND_401

不能直接产生：

STRICT_PASS

STRICT_PASS 唯一计算方式

STRICT_PASS =
HOME_OK
AND
(AUTH_OK OR BACKEND_OK)

其中：

BACKEND_OK

不包含 HTTP 401。


---

P1-02：稳定性每次复测必须重新达到 STRICT_PASS

对应原规则：

§20


权威规则

稳定性测试固定进行 3 次。

每一次复测都必须重新执行完整的严格判定：

HOME_OK
AND
(AUTH_OK OR BACKEND_OK)

即：

一次稳定性成功
=
该次复测 STRICT_PASS

不是简单的：

HTTP 可达

也不是：

HOME 能打开

也不是：

任意一个 endpoint 成功

示例

第1次：STRICT_PASS       → 成功
第2次：仅 HOME_OK        → 失败
第3次：STRICT_PASS       → 成功

结果：

2/3

因此：

STABILITY_PASS = true


---

P1-03：新增 STRICT_CHECK_FAIL

对应原规则：

§13

§16

§17


新增 failure_reason

STRICT_CHECK_FAIL

定义：

> 节点完成核心检测，但未满足 STRICT_PASS，且不存在更具体、更高优先级的失败原因。



例如：

HOME = 200
AUTH = 403
BACKEND = 403

这不是：

OPENAI_BLOCKED

因为并非明确的全核心阻断。

也不是：

CHATGPT_UNREACHABLE

因为服务并非完全不可达。

此时：

failure_reason = STRICT_CHECK_FAIL


---

P1-04：BACKEND_AUTH_REQUIRED 仅属于检测明细

对应原规则：

§12

§16


权威规则

BACKEND_AUTH_REQUIRED

属于：

endpoint_detail / check_detail

而不是：

failure_reason

因此：

check_detail.backend =
{
    status: 401,
    result: "BACKEND_AUTH_REQUIRED"
}

但：

failure_reason

只能从统一 failure_reason 枚举中选择。


---

P1-05：新池为空时绝不替换旧池

对应原规则：

§30

§35J


权威规则

无论原因是什么：

new_pool.count == 0

均：

保留旧池

不得执行原子替换。

只有：

new_pool.count > 0
AND
new_pool 完整验证通过

才能：

原子替换旧池

因此刷新逻辑统一为：

新池为空
    ↓
保留旧池

新池非空
    ↓
完整验证
    ↓
验证通过
    ↓
原子替换

特别说明

即使本轮确实经过完整检测、最终一个合格节点都没有：

new_pool = 0

也：

不清空旧池


---

P1-06：支持国家列表来源必须固定

对应原规则：

§6


权威优先级

官方在线来源
↓
本地缓存
↓
内置最近确认列表

实现时必须把：

OFFICIAL_COUNTRY_SOURCE_URL

作为明确配置项，而不是让 AI 自行选择来源。

同时必须定义：

解析规则
国家代码格式
缓存格式
缓存更新时间
缓存失效处理

国家代码统一

统一使用：

ISO 3166-1 Alpha-2

例如：

US
JP
GB
CA
AU

程序内部统一使用大写。


---

P1-07：ChatGPT 严格检测 endpoint 必须固定

对应原规则：

§10

§11


V5.2 实现时必须定义固定的：

HOME_ENDPOINT
AUTH_ENDPOINT
BACKEND_ENDPOINT

以及每一个 endpoint 的：

HTTP method
允许状态码
最终 URL规则
Content-Type要求
页面/响应特征
超时
重定向处理

不得由不同 AI 根据自己的理解临时增加或替换 endpoint。

页面特征

HOME/AUTH/BACKEND 的成功判断必须同时记录：

HTTP 状态
最终 URL
Content-Type
特征匹配结果

不能仅使用：

status_code == 200

作为唯一判断。


---

P1-08：生产 Mihomo 架构固定

对应原规则：

§25

§33


V5.2 的生产架构明确为：

节点池生成器
      ↓
生产节点配置
      ↓
Mihomo
      ↓
本地代理端口
      ↓
ChatGPT/OpenAI 检测

同时：

订阅服务
      ↓
127.0.0.1:18787
      ↓
/chatgpt.yaml

两者职责不同。

18787

仅负责：

最终订阅文件交付

不是 Mihomo 代理端口。

Mihomo

负责：

节点代理
节点切换
代理连接
出口 IP

生产 Mihomo 必须具有独立：

PID
配置目录
运行目录
日志
代理端口

测试 Mihomo 完全独立。


---

P1-09：稳定性测试固定为 3 次

对应原规则：

§20


权威规则

稳定性测试：

固定 3 次

不允许实现为：

4 次
5 次
N 次

最终判定：

3/3 → PASS
2/3 → PASS
1/3 → FAIL
0/3 → FAIL

不存在：

2/4
3/5
N/M

因此彻底消除：

成功次数 ≥2

与：

成功比例 ≥2/3

之间的冲突。


---

P1-10：V5.2 最终稳定性判定

综合 P1-02 与 P1-09：

每次复测
=
完整 STRICT_PASS

总共：

3 次

最终：

成功次数 >= 2

即可：

STABILITY_PASS

因此：

STRICT_PASS
↓
3次完整STRICT_PASS复测
↓
≥2次成功
↓
STABILITY_PASS
↓
FINAL


---

P1-11：最终池准入条件进一步固化

最终池节点必须同时满足：

stage_status = FINAL
AND
chatgpt_status = STRICT_PASS
AND
stability_pass = true

其中：

stability_pass = true

必须来自：

3 次完整 STRICT_PASS 复测
+
成功 ≥ 2 次

任何以下状态均不得进入最终池：

NETWORK_PASS
BACKEND_AUTH_REQUIRED
CHATGPT_CHECKED
STRICT_CHECK_FAIL
STABILITY_FAIL
REJECTED


---

P1-12：系统级失败不得伪装成节点失败

特别固定：

COUNTRY_LIST_UNAVAILABLE

属于：

系统级刷新失败

不是：

failure_reason

不得把它批量写入所有节点。

发生时：

停止本轮刷新
↓
保留旧池


---

P1-13：刷新发布条件统一

一次刷新最终能够替换生产池，必须同时满足：

country source 可用
AND
检测流程完整执行
AND
new_pool.count > 0
AND
new_pool 验证通过

否则：

保留旧池

最终发布只有一个出口：

validated_non_empty_new_pool
        ↓
atomic_replace


---

P1-14：V5.2 最终判定流程

补丁合并后，完整流程固定为：

获取节点
  ↓
解析
  ↓
去重
  ↓
代理启动
  ↓
获取真实出口 IP
  ↓
国家列表是否可用？
  ├─ 否 → COUNTRY_LIST_UNAVAILABLE
  │       ↓
  │     中止本轮
  │     ↓
  │     保留旧池
  │
  └─ 是
       ↓
真实出口国家是否支持？
  ├─ 否 → REJECTED / UNSUPPORTED_COUNTRY
  │
  └─ 是
       ↓
严格 ChatGPT 检测
       ↓
HOME_OK
AND
(AUTH_OK OR BACKEND_OK)
       ↓
STRICT_PASS
       ↓
稳定性复测 × 3
       ↓
每次均重新执行 STRICT_PASS
       ↓
成功 ≥ 2 次？
  ├─ 否 → REJECTED / STABILITY_FAIL
  │
  └─ 是
       ↓
FINAL
       ↓
加入新池
       ↓
new_pool.count > 0？
  ├─ 否 → 保留旧池
  │
  └─ 是
       ↓
完整验证
       ↓
原子替换
