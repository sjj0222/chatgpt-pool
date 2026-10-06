# chatgpt-pool Android 控制入口（V0.4-4）

> 仅 Web 控制入口，**不是**核心替代品。

## 定位

APK 只是 WebView 壳：加载本机 Web 控制台
`http://127.0.0.1:18888/m`，功能 = 查看状态 / 打开 Web / 手动刷新 / 启停。

**禁止**：APK 直接操作底层（不读数据库、不改配置、不运行 pipeline）。
一切操作经 Web 控制台 API（`POST /api/refresh|start|stop`）。

## 构建（在用户本地完成；沙箱无法产出 APK）

前置：Android Studio（或 gradle 8.5+）+ Android SDK 36（已在 ~/dev-tools）。

```bash
# 复制工程到本地后：
cd chatgpt-pool/apk
./gradlew assembleRelease   # 产物 app/build/outputs/apk/release/app-release.apk
# 或直接在 Android Studio 打开 apk/ 目录，Run / Build APK
```

## 使用

1. Termux 中启动 Web 控制台：`python3 main.py web`（127.0.0.1:18888）。
2. 安装 APK 后打开 → WebView 加载控制台 /m 页。
3. 按钮：手动刷新 / 启动 Mihomo / 停止 Mihomo（结果回显）。

## 安全

- 仅 `127.0.0.1` / `localhost` 允许明文流量（network_security_config），
  不放开其他域。
- 无文件读写、无位置、无通讯录等权限，仅 INTERNET。
- 不保存节点密码/账号/配置（云同步默认关闭且白名单无敏感字段）。
