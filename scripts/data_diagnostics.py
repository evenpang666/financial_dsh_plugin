"""Readable missing-source messages; raw exceptions belong in service logs."""

def readable_issue(value) -> str:
    raw = str(value)
    if "不可用" in raw:
        label = raw[:raw.index("不可用") + 3]
    else:
        label = raw.split("：", 1)[0].split(":", 1)[0][:100]
    if "ProxyError" in raw or "Unable to connect to proxy" in raw:
        reason = "网络代理连接失败"
    elif "Timeout" in raw or "timed out" in raw or "timeout" in raw:
        reason = "请求超时"
    elif "Connection" in raw or "RemoteDisconnected" in raw:
        reason = "网络连接失败"
    else:
        reason = "数据待补充"
    return f"{label}（{reason}）"


def readable_issues(values) -> list[str]:
    return list(dict.fromkeys(readable_issue(value) for value in values))
