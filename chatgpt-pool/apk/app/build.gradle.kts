plugins {
    id("com.android.application")
}

android {
    namespace = "com.chatgptpool.control"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.chatgptpool.control"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.4"
    }
    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    // 仅标准库 WebView，无第三方依赖
}
