"""ChatGPT/OpenAI 严格检测（§10-§14 / P1-01 / P1-03 / P1-04 / P1-07）。

STRICT_PASS = HOME_OK AND (AUTH_OK OR BACKEND_OK)
- Backend 401 → BACKEND_AUTH_REQUIRED（仅网络证据，不参与 BACKEND_OK，P1-01）。
- 单个 endpoint 403 不直接淘汰（§13）；仅当核心检测全部被明确阻断 → OPENAI_BLOCKED。
- WebSocket 是辅助检测，WS 结果不构成否决（§14）。
- 检测明细与 failure_reason 分离（P1-04）。
- V0.2.2：细分原因（SubReason）——全核心阻断 → CHATGPT_BLOCKED；
  全部 endpoint 传输失败且最高优先级为 TIMEOUT → CHATGPT_TIMEOUT。
"""
import logging

from models import ChatgptStatus, FailureReason, SubReason, highest_priority
from probe import ProbeError

log = logging.getLogger("pool.chatgpt")

BLOCK_MARKERS = [
    "not available in your country",
    "unavailable in your country",
    "country is not supported",
    "is not supported in your country",
    "geoblocked",
    "access denied",
]


def _blocked(body):
    b = (body or "").lower()
    return any(m in b for m in BLOCK_MARKERS)


def _probe_home(res):
    out = {"ok": False, "status": None, "final_url": "", "content_type": "",
           "feature_match": False, "blocked": False, "result": "HOME_FAIL"}
    if res is None:
        return out
    out["status"] = res.status
    out["final_url"] = res.final_url or ""
    out["content_type"] = res.content_type or ""
    if res.status is not None and 200 <= res.status < 400:
        body = (res.body or "").lower()
        furl = (res.final_url or "").lower()
        host_ok = any(h in furl for h in (
            "chatgpt.com", "chat.openai.com", "auth.openai.com", "openai.com"))
        page_ok = any(m in body for m in (
            "chatgpt", "openai", "__next_data__", "auth", "log in", "sign in"))
        out["feature_match"] = host_ok and page_ok
        out["ok"] = out["feature_match"]
        if out["ok"]:
            out["result"] = "HOME_OK"
    if res.status == 403 or _blocked(res.body):
        out["blocked"] = True
        out["result"] = "HOME_BLOCKED"
    return out


def _probe_auth(res):
    out = {"ok": False, "status": None, "final_url": "", "content_type": "",
           "feature_match": False, "blocked": False, "result": "AUTH_FAIL"}
    if res is None:
        return out
    out["status"] = res.status
    out["final_url"] = res.final_url or ""
    out["content_type"] = res.content_type or ""
    if res.status is not None and 200 <= res.status < 400:
        body = (res.body or "").lstrip()
        ctype = (res.content_type or "").lower()
        out["feature_match"] = (
            ("json" in ctype or body.startswith("{")) and
            ("csrftoken" in (res.body or "").lower()))
        out["ok"] = out["feature_match"]
        if out["ok"]:
            out["result"] = "AUTH_OK"
    if res.status == 403 or _blocked(res.body):
        out["blocked"] = True
        out["result"] = "AUTH_BLOCKED"
    return out


def _probe_backend(res):
    out = {"ok": False, "status": None, "final_url": "", "content_type": "",
           "feature_match": False, "blocked": False, "kind": "FAIL",
           "result": "BACKEND_FAIL"}
    if res is None:
        return out
    out["status"] = res.status
    out["final_url"] = res.final_url or ""
    out["content_type"] = res.content_type or ""
    if res.status == 401:
        # P1-01：401 只表示 BACKEND_AUTH_REQUIRED，网络可达证据，不算 BACKEND_OK。
        out["kind"] = "AUTH_REQUIRED"
        out["result"] = "BACKEND_AUTH_REQUIRED"
    elif res.status is not None and 200 <= res.status < 400:
        body = (res.body or "").lstrip()
        ctype = (res.content_type or "").lower()
        out["feature_match"] = "json" in ctype or body.startswith("{")
        out["ok"] = out["feature_match"]
        out["kind"] = "OK" if out["ok"] else "FAIL"
        out["result"] = "BACKEND_OK" if out["ok"] else "BACKEND_FAIL"
    elif res.status == 403 or _blocked(res.body):
        out["blocked"] = True
        out["kind"] = "BLOCKED"
        out["result"] = "BACKEND_BLOCKED"
    return out


def run_strict_check(node, probe, cfg):
    """返回 dict：status / strict_pass / network_pass / detail / failure_reason /
    failure_sub / failure_detail（V0.2.2 细分）。"""
    endpoints = {
        "home": cfg.home_endpoint,
        "auth": cfg.auth_endpoint,
        "backend": cfg.backend_endpoint,
    }
    raw = {}
    errs = []          # 顶层 reason
    err_subs = []      # 细分 reason
    err_detail = {}    # endpoint → detail
    for name, url in endpoints.items():
        try:
            raw[name] = probe(url)
        except ProbeError as e:
            raw[name] = None
            errs.append(e.reason)
            err_subs.append(e.sub)
            err_detail[name] = {**e.detail, "url": url}

    home = _probe_home(raw["home"])
    auth = _probe_auth(raw["auth"])
    backend = _probe_backend(raw["backend"])

    detail = {
        "home": home,
        "auth": auth,
        "backend": backend,
        # §14：WebSocket 仅辅助检测，不构成否决条件。
        "ws": {"tested": False, "result": "WS_NOT_TESTED", "auxiliary": True},
        "transport_errors": err_detail,
    }

    home_ok = home["ok"]
    auth_ok = auth["ok"]
    backend_ok = backend["ok"]
    blocked_all = home["blocked"] and auth["blocked"] and backend["blocked"]
    any_response = any(raw[name] is not None for name in endpoints)
    network_evidence = (
        home_ok or auth_ok or backend_ok or
        backend["kind"] == "AUTH_REQUIRED" or any_response)

    strict = home_ok and (auth_ok or backend_ok)

    failure_sub = None
    failure_detail = {"endpoints": {}}
    if strict:
        status = ChatgptStatus.STRICT_PASS
        reason = None
    elif blocked_all:
        status = ChatgptStatus.FAIL
        reason = FailureReason.OPENAI_BLOCKED
        failure_sub = SubReason.CHATGPT_BLOCKED
        failure_detail = {"blocked": {"home": home["result"],
                                      "auth": auth["result"],
                                      "backend": backend["result"]}}
    elif not any_response:
        status = ChatgptStatus.FAIL
        reason = highest_priority(errs) or FailureReason.CHATGPT_UNREACHABLE
        if reason == FailureReason.TIMEOUT:
            # 全部 endpoint 传输失败且超时为主 → 细分 CHATGPT_TIMEOUT（V0.2.2）
            failure_sub = SubReason.CHATGPT_TIMEOUT
        else:
            # 保留与最高优先级顶层原因对应的首个细分原因（如 TLS_TIMEOUT）
            for r, s in zip(errs, err_subs):
                if r == reason:
                    failure_sub = s
                    break
        failure_detail = {"transport_errors": err_detail,
                          "top_sub": ([s.value for s in err_subs] if err_subs else [])}
    else:
        status = (ChatgptStatus.NETWORK_PASS if network_evidence
                  else ChatgptStatus.FAIL)
        reason = FailureReason.STRICT_CHECK_FAIL
        failure_detail = {"home": home, "auth": auth, "backend": backend}

    return {
        "status": status,
        "strict_pass": strict,
        "network_pass": status in (ChatgptStatus.NETWORK_PASS, ChatgptStatus.STRICT_PASS),
        "detail": detail,
        "failure_reason": reason,
        "failure_sub": failure_sub,
        "failure_detail": failure_detail,
    }
