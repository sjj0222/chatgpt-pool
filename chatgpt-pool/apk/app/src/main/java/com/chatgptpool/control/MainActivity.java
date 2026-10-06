package com.chatgptpool.control;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * chatgpt-pool Android 控制入口（V0.4-4）。
 *
 * 仅作为 Web 控制入口：加载本机 Web 控制台
 * http://127.0.0.1:18888/m （查看状态 / 手动刷新 / 启停）。
 *
 * 本 APK 不直接操作底层：不读数据库、不改配置、不运行 pipeline，
 * 一切操作经 Web 控制台 API（POST /api/refresh|start|stop）。
 */
public class MainActivity extends Activity {

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        WebView web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        web.setWebViewClient(new WebViewClient());
        // 本机 Web 控制台；需先在 Termux 中 python3 main.py web
        web.loadUrl("http://127.0.0.1:18888/m");
    }
}
