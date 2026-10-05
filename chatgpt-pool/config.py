"""配置模块（V5.2 + V5.2-P1）。

所有实现规格（endpoint、国家列表来源、端口、超时等）在这里集中定义，
遵循补丁 P1-06 / P1-07：OFFICIAL_COUNTRY_SOURCE_URL 与 HOME/AUTH/BACKEND
endpoint 是明确配置项，不允许执行 AI 自行选择来源或临时更换 endpoint。

优先顺序：命令行/代码覆盖 > 环境变量 > base_dir/config.json > DEFAULTS。
"""
import json
import os

DEFAULTS = {
    # ---- 目录与端口（§2 / §3 / P1-08）----
    "base_dir": "~/chatgpt-pool",           # 生产目录（Termux 固定 ~/chatgpt-pool）
    "test_base_dir": "~/chatgpt-pool-test", # 测试目录
    "prod_server_port": 18787,              # 生产订阅服务端口（§25）
    "test_server_port": 18887,              # 测试订阅服务端口（§3）
    "prod_mihomo_port": 7890,               # 生产 Mihomo 本地代理端口（P1-08：与 18787 分离）
    "prod_engine_port_range": [18000, 18699],  # 生产逐节点检测引擎端口（与 18787/7890 分离）
    "test_port_range": [19000, 19999],      # 测试动态端口（§3）
    "refresh_interval": 7200,               # 刷新周期（§26：7200 秒 = 2 小时）

    # ---- 国家列表（§6 / P1-06）----
    "country_source_url": "https://platform.openai.com/docs/supported-countries",
    "min_online_country_codes": 20,

    # ---- 出口 IP / GeoIP（§5）----
    "geoip_url": "https://ip-api.com/json/",

    # ---- ChatGPT 严格检测 endpoint（§10 / P1-07）----
    "home_endpoint": "https://chat.openai.com/",
    "auth_endpoint": "https://chat.openai.com/api/auth/csrf",
    "backend_endpoint": "https://chat.openai.com/backend-api/me",

    # ---- 网络参数（§11 / P1-07）----
    "connect_timeout": 10,
    "read_timeout": 15,
    "total_timeout": 25,
    "max_redirects": 3,

    # ---- 稳定性（§20 / P1-02 / P1-09 / P1-10）----
    "stability_attempts": 3,     # 固定 3 次
    "stability_min_pass": 2,     # ≥2/3
    "stability_interval": 30,    # 复测间隔（秒）

    # ---- 刷新执行参数 ----
    "concurrency": 5,
    "max_nodes_per_round": 300,

    # ---- 节点来源（可编辑，P1-06 同款机制：来源是配置项不是 AI 自选）----
    # 以下 4 个为 2026-10-05 实测可用的公共列表（404 的已移除）
    "node_source_urls": [
        "https://raw.githubusercontent.com/mahdibland/ShadowsocksAggregator/master/Eternity.txt",
        "https://raw.githubusercontent.com/freefq/free/master/v2",
        "https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/v2ray.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    ],

    # ---- Mihomo ----
    "mihomo_bin": "mihomo",
    # 生产常驻 Mihomo 的 external-controller（仅 127.0.0.1，用于热重载/健康检查；
    # 端口矩阵之外独立分配，避免与 18787/7890/18000-18699 冲突）
    "mihomo_api_port": 9090,
    "mihomo_log_level": "info",   # 生产常驻日志级别（检测引擎用独立 warn 级配置）
    "mihomo_start_timeout": 20,   # 启动就绪等待（秒）
    "mihomo_group_url": "https://www.gstatic.com/generate_204",  # 代理层 url-test 健康检查（非 ChatGPT 严格检测，§10 不受影响）

    # ---- 其他 ----
    "user_agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",

    # ---- 内置最近确认列表（§6 三级回退的第三级；需随官方来源更新）----
    "builtin_countries": [
        "US", "JP", "GB", "CA", "AU", "DE", "FR", "IT", "ES", "NL",
        "SE", "NO", "DK", "FI", "PL", "IE", "AT", "BE", "CH", "PT",
        "KR", "SG", "IN", "BR", "MX", "AR", "CL", "CO", "PE", "NZ",
        "IL", "AE", "ZA", "ID", "MY", "TH", "PH", "VN", "TR", "SA",
        "EG", "MA", "NG", "KE", "GH", "CZ", "HU", "RO", "BG", "GR",
        "HR", "SK", "SI", "LT", "LV", "EE", "CY", "MT", "LU", "IS",
    ],
}

# 环境变量覆盖
_ENV_MAP = {
    "POOL_BASE": "base_dir",
    "POOL_TEST_BASE": "test_base_dir",
    "POOL_PORT": "prod_server_port",
    "POOL_TEST_PORT": "test_server_port",
    "POOL_INTERVAL": "refresh_interval",
}


class Config:
    """不可变配置快照；测试用 Config(base_dir=..., ...) 或 for_test() 构造。"""

    def __init__(self, config_file=None, **kw):
        data = dict(DEFAULTS)
        if config_file and os.path.isfile(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        data.update(kw)
        for env, key in _ENV_MAP.items():
            v = os.environ.get(env)
            if v is not None:
                data[key] = int(v) if isinstance(data.get(key), int) else v

        self.base_dir = os.path.expanduser(data["base_dir"])
        self.test_base_dir = os.path.expanduser(data["test_base_dir"])
        self.prod_server_port = int(data["prod_server_port"])
        self.test_server_port = int(data["test_server_port"])
        self.prod_mihomo_port = int(data["prod_mihomo_port"])
        self.test_port_range = [int(x) for x in data["test_port_range"]]
        self.prod_engine_port_range = [int(x) for x in data["prod_engine_port_range"]]
        self.refresh_interval = int(data["refresh_interval"])
        self.country_source_url = data["country_source_url"]
        self.min_online_country_codes = int(data["min_online_country_codes"])
        self.geoip_url = data["geoip_url"]
        self.home_endpoint = data["home_endpoint"]
        self.auth_endpoint = data["auth_endpoint"]
        self.backend_endpoint = data["backend_endpoint"]
        self.connect_timeout = int(data["connect_timeout"])
        self.read_timeout = int(data["read_timeout"])
        self.total_timeout = int(data["total_timeout"])
        self.max_redirects = int(data["max_redirects"])
        self.stability_attempts = int(data["stability_attempts"])
        self.stability_min_pass = int(data["stability_min_pass"])
        self.stability_interval = int(data["stability_interval"])
        self.concurrency = int(data["concurrency"])
        self.max_nodes_per_round = int(data["max_nodes_per_round"])
        self.node_source_urls = list(data["node_source_urls"])
        self.mihomo_bin = data["mihomo_bin"]
        self.mihomo_api_port = int(data["mihomo_api_port"])
        self.mihomo_log_level = data["mihomo_log_level"]
        self.mihomo_start_timeout = int(data["mihomo_start_timeout"])
        self.mihomo_group_url = data["mihomo_group_url"]
        self.user_agent = data["user_agent"]
        self.builtin_countries = list(data["builtin_countries"])

    @classmethod
    def for_test(cls, base=None, **kw):
        """测试专用：目录全部落在测试目录内，端口使用测试端口。"""
        base = base or os.environ.get("POOL_TEST_BASE", "~/chatgpt-pool-test")
        kw.setdefault("base_dir", base)
        kw.setdefault("test_base_dir", base)
        kw.setdefault("prod_server_port", 18787)  # 保持定义，但测试绝不绑定
        kw.setdefault("test_server_port", 18887)
        return cls(**kw)

    @property
    def country_cache_path(self):
        return os.path.join(self.base_dir, "supported_countries.json")

    @property
    def pool_path(self):
        return os.path.join(self.base_dir, "chatgpt.yaml")

    @property
    def tmp_pool_path(self):
        return os.path.join(self.base_dir, "chatgpt.yaml.tmp")

    def sub(self, name):
        return os.path.join(self.base_dir, name)

    def to_json(self):
        return json.dumps(DEFAULTS, ensure_ascii=False, indent=2)
