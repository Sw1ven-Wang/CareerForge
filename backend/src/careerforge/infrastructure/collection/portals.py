"""Reviewed recruitment origins; client arguments never extend the host allowlist."""

from urllib.parse import quote, urlparse

PORTALS = {
    "boss": (
        "BOSS直聘",
        "https://www.zhipin.com/",
        ["www.zhipin.com", "m.zhipin.com"],
        "https://www.zhipin.com/web/geek/job?query={query}",
    ),
    "nowcoder": (
        "牛客",
        "https://www.nowcoder.com/jobs/recommend/campus",
        ["www.nowcoder.com", "mnowpick.nowcoder.com"],
        "https://www.nowcoder.com/jobs/fulltime/center",
    ),
    "liepin": (
        "猎聘",
        "https://www.liepin.com/",
        ["www.liepin.com"],
        "https://www.liepin.com/zhaopin/?key={query}",
    ),
    "tencent": (
        "腾讯招聘官网",
        "https://careers.tencent.com/",
        ["careers.tencent.com"],
        "https://careers.tencent.com/search.html?keyword={query}",
    ),
    "baidu": (
        "百度招聘官网",
        "https://talent.baidu.com/",
        ["talent.baidu.com"],
        "https://talent.baidu.com/jobs/social",
    ),
    "bytedance": (
        "字节跳动招聘官网",
        "https://jobs.bytedance.com/",
        ["jobs.bytedance.com"],
        "https://jobs.bytedance.com/experienced/position?keywords={query}",
    ),
    "alibaba": (
        "阿里巴巴招聘官网",
        "https://talent.alibaba.com/",
        ["talent.alibaba.com"],
        "https://talent.alibaba.com/",
    ),
    "meituan": (
        "美团招聘官网",
        "https://career.meituan.com/web/home",
        ["career.meituan.com", "zhaopin.meituan.com", "job.meituan.com"],
        "https://career.meituan.com/web/home",
    ),
    "jd": (
        "京东招聘官网",
        "https://zhaopin.jd.com/home",
        ["zhaopin.jd.com", "campus.jd.com"],
        "https://zhaopin.jd.com/home",
    ),
    "netease": (
        "网易招聘官网",
        "https://hr.163.com/",
        ["hr.163.com", "campus.163.com", "game.campus.163.com"],
        "https://hr.163.com/",
    ),
    "kuaishou": (
        "快手招聘官网",
        "https://zhaopin.kuaishou.cn/",
        ["zhaopin.kuaishou.cn"],
        "https://zhaopin.kuaishou.cn/",
    ),
}


def identify(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("仅支持已登记招聘站点的 HTTPS 地址")
    for key, portal in PORTALS.items():
        if parsed.hostname in portal[2]:
            return key
    raise ValueError("该招聘域名尚未登记，请先核对官方域名并添加适配器")


def catalog(query="Agent"):
    return [
        {
            "id": key,
            "name": p[0],
            "home": p[1],
            "search_url": p[3].format(query=quote(query)),
            "keyword_query": key == "tencent",
            "detail_adapter": key in {"nowcoder", "liepin", "tencent", "baidu"},
        }
        for key, p in PORTALS.items()
    ]
