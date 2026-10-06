# chatgpt-pool Android 控制入口（V0.4-4）
# 仅 Web 控制入口：查看状态 / 打开 Web / 手动刷新 / 启停。
# 禁止直接操作底层（不读数据库、不改配置、不跑 pipeline）。
rootProject.name = "chatgpt-pool-control"
include(":app")
