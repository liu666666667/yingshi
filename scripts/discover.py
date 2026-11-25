#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yingshi 每日全网源发现（纯标准库实现，本地 / GitHub Actions 通用）
================================================================
设计目标：仓库自己每天"全网搜索新源"，无需人工维护任何清单。

四条搜索通道（全部自动，单通道失败自动跳过不影响整体）：
  A. seeds    内置种子：用户自己的仓库（yingshicang）+ 常年活跃聚合清单（仅冷启动）
  B. github   GitHub 仓库搜索（配置 GITHUB_TOKEN 时走认证，配额更高）
              搜索最近更新的相关仓库 → 拉取 README → 提取候选接口地址
  C. web      Bing(RSS) + DuckDuckGo(HTML) 网页搜索 → 落地页提取候选地址
  D. gitee    Gitee 仓库搜索（国内可达的兜底通道）→ README 提取

候选规则：URL 以 .json/.txt 结尾，或 URL 含 tvbox/影视/iptv/接口 等特征词。
校验：App UA 轮换抓取（防伪装图片反爬）→ JSON / m3u 解析 →
      点播站点数 ≥3、直播频道数 ≥20 才算有效；多仓自动标记 expand。

自生长与自清理（零人工维护）：
  - 新源写入 config/auto_sources.json（点播）/ config/auto_lives.json（直播），
    记录 channel / score / last_ok / fail_streak；
  - 每次运行对存量源做健康检查，连续 7 次失效自动淘汰；
  - 总量上限：点播 80 / 直播 30，超限按失效次数与得分自动裁剪。

输出：output/discovery_report.json（各通道统计，供导航页/README 展示）。
用法：python scripts/discover.py
"""
import base64
import datetime
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import (fetch_text_with_fallback, parse_json_lenient,
                    is_tvbox_config, is_multi_warehouse, http_get as http_get_text, APP_UAS)
# 注：update.http_get 返回 (文本, 是否文本) 且支持 ua/xrw（用于候选探测 probe_url）；
# 本文件下方另有一个同名 http_get 返回 bytes（供 GitHub/web API 抓取用）。两者签名不同，
# 故给 update 版起别名 http_get_text，避免被本地同名定义覆盖导致 probe_url 全部静默失败。

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
OUTPUT_DIR = os.path.join(ROOT, "output")
AUTO_SOURCES_FILE = os.path.join(CONFIG_DIR, "auto_sources.json")
AUTO_LIVES_FILE = os.path.join(CONFIG_DIR, "auto_lives.json")
DISC_CFG_FILE = os.path.join(CONFIG_DIR, "discovery.json")

TZ = datetime.timezone(datetime.timedelta(hours=8))
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0"

GH_API = "https://api.github.com"
GITEE_API = "https://gitee.com/api/v5"

GH_QUERIES = [
    "tvbox 接口", "影视仓 接口", "TVBox 配置 json", "TVBox config",
    "饭太硬 接口", "TVBox 直播源 github",
    "tvbox nsfw", "tvbox 18+",
]
# GitHub 代码搜索：搜全网 TVBox 配置 JSON 文件（与参考项目同口径；
# 仓库名/路径命中源级归集词的源在 update.py 吸收时整源分流）。扩容：8 组×30 条
GH_CODE_QUERIES = [
    "spider parses extension:json",
    "\"sites\" \"spiders\" extension:json",
    "\"sites\" \"parses\" extension:json",
    "影视仓 extension:json",
    "tvbox 订阅 extension:json",
    "tvbox 接口 extension:json",
    "饭太硬 extension:json",
    "csp_ 影视 extension:json",
]
WEB_QUERIES = [
    "TVBox接口 最新 地址", "影视仓 接口 github", "TVBox 配置 json",
    "饭太硬 接口地址", "TVBox 直播源 json", "影视仓 仓库 接口地址",
    "tvbox 🔞 json github", "tvbox 18+ json github", "影视仓 成人源 github",
]
GITEE_QUERIES = ["tvbox"]  # Gitee 匿名 API 结果常为空，仅兑底；首查询空则快速失败

# 内置种子：用户自己的仓库 + 活跃聚合清单（仅冷启动，之后靠自生长清单滚动）
# 注：yingshicang 已由 config/own_sources.json 单独维护（own 组），种子通道不重复收录
SEED_REPOS = []
SEED_LISTS = [
    "https://raw.githubusercontent.com/lubin776/tvbox-api-backup/main/list.txt",
    "https://raw.githubusercontent.com/lubin776/tvbox-api-backup/main/apilinks.txt",
]
SEED_LOWER = {f.lower() for f in SEED_REPOS}

MAX_NEW_CONFIG = 30   # 单次纳入新点播源上限（扩容）
MAX_NEW_LIVE = 10     # 单次纳入新直播源上限
CAP_CONFIG = 150      # 自动点播源总量上限
CAP_LIVE = 40         # 自动直播源总量上限
FAIL_LIMIT = 7        # 连续失效 N 次自动淘汰
MAX_PROBE = 240       # 单次探测新候选上限（扩容）
# 全局时间预算：云端（Actions，网络快）通过 DISC_TIME_BUDGET 环境变量放大，本地默认 270s
TIME_BUDGET = int(os.environ.get("DISC_TIME_BUDGET", "270"))

# 需要登录/进群/付费才能用的源特征词（命中则拒绝纳入，可在 discovery.json 外置调整）
LOGIN_KW = ("卡密", "授权码", "邀请码", "激活码", "请登录", "需登录", "登录后",
            "付费开通", "扫码登录", "扫码获取", "进群获取", "加群获取", "加群交流")
LOGIN_KWS = list(LOGIN_KW)  # main() 里从 discovery.json 覆盖

# 命中这些特征的源一律不要（新源拒收 + 存量立即淘汰）：
# 1) 图片伪装成爬虫引擎的 spider（.png/.jpg...）——影视仓加载时弹"下载 GO 组件/初始化本地代理/申请本地文件权限"
# 2) spider 地址里直接带"本地代理/go 组件"字样的魔改包
BAD_SPIDER_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
GO_PROXY_KW = ("本地代理", "下载go", "下载 go", "go组件", "go 组件", "初始化代理")
DISABLED = set()   # 命中禁用特征的 URL（norm 后），存量源据此立即淘汰


def _disable_reason(d, text):
    """返回禁用原因字符串，正常源返回 None。登录/付费/GO伪装spider 全拒。"""
    if any(k in text[:30000] for k in LOGIN_KWS):
        return "login"
    sp = d.get("spider") if isinstance(d, dict) else None
    if isinstance(sp, str) and sp:
        head = sp.split(";", 1)[0].split("?", 1)[0].strip().lower()
        if head.endswith(BAD_SPIDER_EXTS):
            return "image-spider"
        if any(k in sp.lower() for k in GO_PROXY_KW):
            return "go-proxy"
    return None

GH_BLOB_RE = re.compile(
    r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:blob|raw)/'
    r'([^/\s"\'<>]+)/([^\s"\'<>）)\]，。；、]+)')
URL_RE = re.compile(r'https?://[^\s"\'<>|\\）)\]，。；、]+')
PROXY_PREFIX = re.compile(
    r'^https?://(?:[a-z0-9.-]*ghproxy[a-z0-9.-]*\.(?:com|net|org|link)|gh-proxy\.(?:com|org|link)|ghfast\.top|ghproxy\.(?:net|link)/)/',
    re.I)
SOFT_PAT = re.compile(r'(tvbox|tv_box|影视仓|电视仓|影视|iptv|饭太硬|菜妮丝|摸鱼|宝盒|接口|ysc|catvod)',
                      re.I)
BAD_EXT = ('.jar', '.py', '.js', '.apk', '.zip', '.rar', '.md', '.html', '.htm',
           '.css', '.png', '.jpg', '.jpeg', '.gif', '.so', '.sh', '.woff', '.ts')


T0 = time.time()


def now_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def http_get(url, timeout=12, headers=None, max_bytes=800_000):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    # 非字母数字路径/查询编码（中文路径直接请求会崩溃）
    try:
        p = urllib.parse.urlsplit(url)
        if any(ord(c) > 127 for c in (p.path or "") + (p.query or "")):
            url = urllib.parse.urlunsplit((p.scheme, p.netloc,
                                           urllib.parse.quote(p.path, safe="/%"),
                                           urllib.parse.quote(p.query, safe="=&?/%+~"),
                                           p.fragment))
    except Exception:
        pass
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read(max_bytes)


def gh_headers(token):
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def norm(url):
    try:
        p = urllib.parse.urlsplit(url)
        return (p.netloc.lower(), p.path.rstrip("/"))
    except Exception:
        return ("", "")


def unwrap_proxy(u):
    """去掉 gh-proxy 等镜像前缀，还原真实地址（避免把镜像 URL 存成源）。"""
    m = PROXY_PREFIX.match(u)
    if m:
        rest = u[m.end():]
        if rest.startswith(("http://", "https://")):
            return rest
        if rest.startswith(("raw.githubusercontent.com", "github.com",
                            "gist.githubusercontent.com", "gitlab.com")):
            return "https://" + rest
    return u


def normalize_cand(u):
    """候选 URL 规范化：解 bing 重定向、去镜像前缀、blob→raw、压多斜杠。"""
    u = bing_decode(u.rstrip(').,;\'"”’'))
    u = unwrap_proxy(gh_blob_to_raw(u))
    u = re.sub(r'(https?://[^/?#]+)//+', r'\1/', u)
    return u


def bing_decode(u):
    """解码 bing.com/ck/a?...&u=a1<base64url> 重定向链接，拿真实落地页。"""
    if 'bing.com/ck/a' not in u:
        return u
    try:
        qs = urllib.parse.urlsplit(u).query
        enc = urllib.parse.parse_qs(qs).get('u', [''])[0]
        if enc.startswith('a1') and len(enc) > 3:
            enc = enc[2:]
            pad = '=' * (-len(enc) % 4)
            return base64.urlsafe_b64decode(enc + pad).decode('utf-8', 'ignore')
    except Exception:
        pass
    return u


def gh_blob_to_raw(u):
    """github.com/u/r/blob/branch/path → raw.githubusercontent.com/u/r/branch/path"""
    m = GH_BLOB_RE.match(u)
    if m:
        return "https://raw.githubusercontent.com/%s/%s/%s/%s" % m.groups()
    return u


def extract_candidates(text):
    """从任意网页/清单/README 文本中提取候选接口地址。"""
    out = set()
    for m in GH_BLOB_RE.finditer(text):
        path = m.group(4).rstrip(').,;\'"”’')
        if path.lower().endswith((".json", ".txt")):
            out.add("https://raw.githubusercontent.com/%s/%s/%s/%s" % (
                m.group(1), m.group(2), m.group(3), path))
    for u in URL_RE.findall(text):
        u = unwrap_proxy(gh_blob_to_raw(u.rstrip(').,;\'"”’')))
        low = u.lower().split("?")[0]
        if low.endswith(BAD_EXT):
            continue
        if "/yingshi/" in low or low.endswith("/yingshi"):
            continue  # 自家产物地址，避免回环收录
        if low.endswith((".json", ".txt")) or SOFT_PAT.search(low):
            out.add(u)
    return out


def _over_budget():
    return time.time() - T0 > TIME_BUDGET


# ---- 通道 A：种子 ----
def chan_seeds(token, stats):
    if _over_budget():
        print("[DISC/seeds] 超时间预算，跳过")
        return set()
    urls = set()
    headers = gh_headers(token)
    for full in SEED_REPOS:
        try:
            data = json.loads(http_get(GH_API + "/repos/%s/contents/" % full,
                                       headers=headers, timeout=15).decode("utf-8", "ignore"))
            if isinstance(data, list):
                for it in data:
                    nm, dl = it.get("name", ""), it.get("download_url")
                    if not dl:
                        continue
                    low = nm.lower()
                    if low.endswith((".json", ".txt")):
                        urls.add(unwrap_proxy(dl))
                    elif low == "readme.md":
                        try:
                            urls |= extract_candidates(
                                http_get(dl, timeout=10, max_bytes=400_000).decode("utf-8", "ignore"))
                        except Exception:
                            pass
                stats["seeds"]["repos"] += 1
        except Exception as e:
            print("[DISC/seeds] 仓库读取失败 %s : %s" % (full, str(e)[:40]))
    for m in SEED_LISTS:
        got = False
        for cand in (m, "https://gh-proxy.com/" + m, "https://ghproxy.net/" + m):
            try:
                t = http_get(cand, timeout=12, max_bytes=500_000).decode("utf-8", "ignore")
                urls |= extract_candidates(t)
                stats["seeds"]["lists"] += 1
                got = True
                break
            except Exception:
                continue
        if not got:
            print("[DISC/seeds] 清单失败 %s（直连与镜像均不可达）" % m)
    stats["seeds"]["candidates"] = len(urls)
    return urls


# ---- 通道 B2：GitHub 代码搜索（需 token；搜文件内容里的 TVBox 配置键） ----
OWN_URL_MARK = "liu666666667/yingshicang"  # own 组源特征：种子/搜索发现的候选一律不收进 auto
OWN_REPO_PREFIXES = ("liu666666667/",)     # 搜索发现时跳过自己账号下的全部仓库（own 组源由 own_sources.json 单独维护，不重复纳入 auto）


def _is_own_candidate(u):
    return OWN_URL_MARK in u.lower()

def chan_gh_code(token, stats):
    """GitHub /search/code：全网搜 TVBox 配置 JSON 文件，拉取候选。
    使用运行环境自带 token（Actions 内置 GITHUB_TOKEN，非个人 PAT）。"""
    if not token:
        print("[DISC/ghcode] 无 GITHUB_TOKEN，跳过代码搜索通道")
        return set()
    if _over_budget():
        print("[DISC/ghcode] 超时间预算，跳过")
        return set()
    headers = gh_headers(token)
    cands, seen = [], set()
    errors = []
    for q in GH_CODE_QUERIES:
        if _over_budget():
            print("[DISC/ghcode] 超时间预算，中止剩余查询")
            break
        got = False
        for attempt in (1, 2, 3):
            try:
                api = GH_API + "/search/code?q=" + urllib.parse.quote(q) + "&per_page=30"
                data = json.loads(http_get(api, headers=headers, timeout=20).decode("utf-8", "ignore"))
                for it in data.get("items", []) or []:
                    full = (it.get("repository") or {}).get("full_name", "")
                    fp = it.get("path", "")
                    if not full or not fp:
                        continue
                    if full.lower().startswith(OWN_REPO_PREFIXES) or full.lower() in SEED_LOWER:
                        continue
                    rp = full + "::" + fp
                    if rp not in seen:
                        seen.add(rp)
                        cands.append((full, fp))
                stats["ghcode"]["queries"] += 1
                got = True
                break
            except Exception as e:
                msg = "%s : %s" % (q[:24], str(e)[:60])
                if "429" in str(e) and attempt < 3 and not _over_budget():
                    # 代码搜索限流严格（认证 10 次/分）：优先按响应头 Retry-After / reset 退避，
                    # 否则指数退避 15/30s，且不超过总时间预算，避免整条通道被 429 吃掉。
                    wait = 15 * attempt
                    hd = getattr(e, "headers", None)
                    if hd:
                        try:
                            if hd.get("Retry-After"):
                                wait = max(wait, int(hd.get("Retry-After")))
                            if hd.get("x-ratelimit-reset"):
                                wait = max(wait, int(hd.get("x-ratelimit-reset")) - int(time.time()) + 2)
                        except Exception:
                            pass
                    wait = min(wait, 60)
                    print("[DISC/ghcode] 限流，等待 %ss 重试(%d/3): %s" % (wait, attempt, q[:24]))
                    time.sleep(wait)
                    continue
                errors.append(msg)
                print("[DISC/ghcode] 搜索失败 %s" % msg)
                break
        time.sleep(15)  # 代码搜索限流严格，组间停顿（Actions IP 池易 429）
    stats["ghcode"]["files"] = len(cands)
    if errors:
        stats["ghcode"]["errors"] = errors[:8]

    def fetch_one(item):
        full, fp = item
        try:
            enc = urllib.parse.quote(fp, safe="/")
            d = json.loads(http_get(GH_API + "/repos/%s/contents/%s" % (full, enc),
                                    headers=headers, timeout=12).decode("utf-8", "ignore"))
            if not isinstance(d, dict):
                return set()
            out = set()
            dl = d.get("download_url")
            if dl and str(fp).lower().endswith((".json", ".txt")):
                out.add(dl)
            if d.get("encoding") == "base64" and d.get("content"):
                try:
                    text = base64.b64decode(d["content"]).decode("utf-8", "replace")
                    out |= extract_candidates(text[:300000])
                except Exception:
                    pass
            return out
        except Exception:
            return set()

    urls = set()
    if cands:
        with ThreadPoolExecutor(max_workers=10) as ex:
            for s in ex.map(fetch_one, cands[:150]):
                urls |= s
                if _over_budget():
                    break
    stats["ghcode"]["candidates"] = len(urls)
    return urls


# ---- 通道 B：GitHub 仓库搜索 ----
def chan_github(token, stats):
    if _over_budget():
        print("[DISC/github] 超时间预算，跳过")
        return set()
    urls = set()
    headers = gh_headers(token)
    repos = []
    for q in GH_QUERIES:
        api = GH_API + "/search/repositories?q=" + urllib.parse.quote(q) + \
              "&sort=updated&order=desc&per_page=20"
        try:
            data = json.loads(http_get(api, headers=headers, timeout=15).decode("utf-8", "ignore"))
            for it in data.get("items", []):
                full, pushed = it.get("full_name"), it.get("pushed_at", "")
                if full:
                    repos.append((full, pushed))
            stats["github"]["queries"] += 1
        except Exception as e:
            print("[DISC/github] 搜索失败 %s : %s" % (q, str(e)[:50]))
        time.sleep(7 if not token else 2.2)
    uniq = {}
    for full, pushed in repos:
        if full.lower() in SEED_LOWER:
            continue
        if full not in uniq or pushed > uniq[full]:
            uniq[full] = pushed
    ranked = [f for f, _ in sorted(uniq.items(), key=lambda kv: kv[1], reverse=True)[:18]]
    stats["github"]["repos"] = len(ranked)

    def readme_cands(full):
        for br in ("main", "master", "HEAD"):
            try:
                t = http_get("https://raw.githubusercontent.com/%s/%s/README.md" % (full, br),
                             timeout=10, max_bytes=400_000)
                return extract_candidates(t.decode("utf-8", "ignore"))
            except Exception:
                continue
        return set()

    if ranked:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for s in ex.map(readme_cands, ranked):
                urls |= s
    stats["github"]["candidates"] = len(urls)
    return urls


# ---- 通道 C：网页搜索引擎 ----
def chan_web(stats):
    if _over_budget():
        print("[DISC/web] 超时间预算，跳过")
        return set()
    urls, pages = set(), set()
    for q in WEB_QUERIES:
        try:
            xml = http_get("https://www.bing.com/search?q=" + urllib.parse.quote(q) +
                           "&format=rss&count=20", timeout=12).decode("utf-8", "ignore")
            for m in re.finditer(r"<link>([^<]+)</link>", xml):
                u = bing_decode(m.group(1).strip())
                if u.startswith("http"):
                    pages.add(u)
            stats["web"]["queries"] += 1
        except Exception as e:
            print("[DISC/web] bing 失败 %s : %s" % (q, str(e)[:40]))
        try:
            html = http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q),
                            timeout=12).decode("utf-8", "ignore")
            for m in re.finditer(r'uddg=([^&"\']+)', html):
                u = urllib.parse.unquote(m.group(1))
                if u.startswith("http"):
                    pages.add(u)
        except Exception:
            pass
        time.sleep(1.2)
    stats["web"]["pages"] = len(pages)

    def page_cands(page):
        try:
            t = http_get(page, timeout=8, max_bytes=500_000).decode("utf-8", "ignore")
            return extract_candidates(t)
        except Exception:
            return set()

    if pages:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for s in ex.map(page_cands, list(pages)[:18]):
                urls |= s
                if _over_budget():
                    break
    stats["web"]["candidates"] = len(urls)
    return urls


# ---- 通道 D：Gitee ----
def chan_gitee(stats):
    if _over_budget():
        print("[DISC/gitee] 超时间预算，跳过")
        return set()
    urls, repos = set(), []
    for q in GITEE_QUERIES:
        api = GITEE_API + "/search/repositories?q=" + urllib.parse.quote(q) + \
              "&order=desc&per_page=15"
        try:
            data = json.loads(http_get(api, timeout=12).decode("utf-8", "ignore"))
            if isinstance(data, list) and data:
                for it in data:
                    ns = it.get("namespace")
                    fn = it.get("full_name") or (
                        (ns.get("path", "") + "/" + it.get("path", ""))
                        if isinstance(ns, dict) else "")
                    if fn and "/" in fn:
                        repos.append(fn.strip("/"))
                stats["gitee"]["queries"] += 1
            else:
                # 匿名场景下 Gitee 常返回空列表，快速失败不浪费时间
                break
        except Exception as e:
            print("[DISC/gitee] 搜索失败 %s : %s" % (q, str(e)[:40]))
        time.sleep(1.2)
    repos = list(dict.fromkeys(repos))[:10]
    stats["gitee"]["repos"] = len(repos)

    def readme_cands(full):
        for br in ("master", "main"):
            try:
                t = http_get("https://gitee.com/%s/raw/%s/README.md" % (full, br),
                             timeout=10, max_bytes=400_000)
                return extract_candidates(t.decode("utf-8", "ignore"))
            except Exception:
                continue
        return set()

    if repos:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for s in ex.map(readme_cands, repos):
                urls |= s
    stats["gitee"]["candidates"] = len(urls)
    return urls


# ---- 可选通道：人工指定的额外聚合清单 ----
def chan_manifests(manifests, stats):
    urls = set()
    for m in manifests[:6]:
        try:
            t = fetch_text_with_fallback([m], retries=0)[2]
            urls |= extract_candidates(t)
            stats["manifests"]["lists"] += 1
        except Exception as e:
            print("[DISC/manifests] 失败 %s : %s" % (m, str(e)[:40]))
    stats["manifests"]["candidates"] = len(urls)
    return urls


# ---- 探活校验 ----
def _config_fp(d):
    """站点集合指纹：同内容的镜像（ghfast/jsdelivr/ghproxy 同一仓库）指纹相同，用于去重。"""
    sites = d.get("sites") or d.get("urls") or []
    idents = sorted(str(s.get("key") or s.get("url") or s.get("name") or "")
                    for s in sites if isinstance(s, dict))[:300]
    return hashlib.md5(("|".join(idents)).encode("utf-8")).hexdigest()[:12]


def _m3u_fp(text):
    urls = sorted(l.strip() for l in text.splitlines()
                 if "://" in l and not l.strip().startswith("#"))[:80]
    return hashlib.md5(("|".join(urls)).encode("utf-8")).hexdigest()[:12]


def probe_url(url):
    """返回 (kind, n, fp) 或 None。短超时探测（8s×2 UA），防单条卡死。
    命中登录/付费特征词的源直接拒绝（去除需要登录的那些）。"""
    text = None
    for ua, xrw in APP_UAS[:2]:
        try:
            t, is_text = http_get_text(url, timeout=8, ua=ua, xrw=xrw)
            if is_text:
                text = t
                break
        except Exception:
            continue
    if not text:
        return None
    head = text[:6000]
    if "#EXTM3U" in head or "#EXTINF" in head or "#genre#" in head:
        urls = [l.strip() for l in text.splitlines()
                if "://" in l and not l.strip().startswith("#")]
        if len(urls) < 20:
            return None
        return ("live", len(urls), _m3u_fp(text))
    try:
        d = parse_json_lenient(text)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    # 登录/付费/GO本地代理伪装spider：一律拒绝，存量也标记立即淘汰
    reason = _disable_reason(d, text)
    if reason:
        DISABLED.add(norm(url))
        print("[DISC] 拒绝（%s）: %s" % (reason, url[:70]))
        return None
    if is_multi_warehouse(d):
        n = len(d.get("urls") or [])
        return ("multi", n, _config_fp(d)) if n >= 3 else None
    if is_tvbox_config(d):
        n = len(d.get("sites") or [])
        return ("single", n, _config_fp(d)) if n >= 3 else None
    return None


def make_entry(url, kind, n, channel):
    p = urllib.parse.urlsplit(url)
    host = p.netloc or "unknown"
    # 名称带完整路径（截断 64 字），便于区分同域名下的多个源
    name = (host + p.path)[:64] or host
    sid = "auto_%s_%s" % (re.sub(r"[^a-z0-9]", "", host.lower())[:14] or "src",
                          hashlib.md5(url.encode("utf-8")).hexdigest()[:6])
    e = {"id": sid, "name": name, "type": kind, "urls": [url], "channel": channel,
         "first_seen": now_str(), "last_ok": now_str(), "fail_streak": 0, "score": n}
    if kind == "multi":
        e["expand"] = True
    return e


# ---- 存量状态 ----
def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("sources"), list):
            return d
    except Exception:
        pass
    return {"sources": []}


def _save(path, d):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _ids_from(path):
    ids = set()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        for s in d.get("sources", []) or []:
            if s.get("id"):
                ids.add(s["id"])
    except Exception:
        pass
    return ids


def collect_known():
    """sources/live_sources/own_sources/auto_sources/auto_lives 中全部已知 URL（归一化）。"""
    known = set()

    def add_file(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            for s in d.get("sources", []) or []:
                for u in s.get("urls", []) or []:
                    known.add(norm(u))
        except Exception:
            pass

    add_file(os.path.join(CONFIG_DIR, "sources.json"))
    add_file(os.path.join(CONFIG_DIR, "own_sources.json"))
    add_file(os.path.join(CONFIG_DIR, "live_sources.json"))
    add_file(AUTO_SOURCES_FILE)
    add_file(AUTO_LIVES_FILE)
    return known


def health_check(entries):
    """存量源探活（分批 + 预算控制）：更新 last_ok / fail_streak / score。返回存活数。"""
    if not entries:
        return 0

    def check(e):
        u = (e.get("urls") or [""])[0]
        if not u:
            return False, 0
        if norm(u) in DISABLED:
            return False, FAIL_LIMIT   # 命中禁用特征，存量立即淘汰
        r = probe_url(u)
        return (r is not None, r[1] if r else 0)

    alive = 0
    B = 12
    for i in range(0, len(entries), B):
        if _over_budget():
            print("[DISC] 超时间预算，剩余存量源本轮跳过健康检查")
            break
        with ThreadPoolExecutor(max_workers=10) as ex:
            res = list(ex.map(check, entries[i:i + B]))
        for e, (ok, n) in zip(entries[i:i + B], res):
            if ok:
                alive += 1
                e["last_ok"] = now_str()
                e["fail_streak"] = 0
                e["score"] = n
            else:
                if n == FAIL_LIMIT:
                    e["fail_streak"] = FAIL_LIMIT   # 命中禁用特征，立即淘汰
                else:
                    e["fail_streak"] = int(e.get("fail_streak", 0)) + 1
    return alive


def main():
    global T0, LOGIN_KWS
    T0 = time.time()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    cfg = {}
    if os.path.exists(DISC_CFG_FILE):
        try:
            with open(DISC_CFG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if cfg.get("enabled") is False:
        print("[DISC] discovery.json 中 enabled=false，跳过全网搜索")
        return 0
    max_new_cfg = int(cfg.get("max_new_per_run", MAX_NEW_CONFIG))
    max_new_live = int(cfg.get("max_new_lives", MAX_NEW_LIVE))
    extra_manifests = cfg.get("extra_manifests") or []
    if cfg.get("login_block_keywords"):
        LOGIN_KWS = [w for w in cfg["login_block_keywords"] if w]

    stats = {"github": {"queries": 0, "repos": 0, "candidates": 0},
             "ghcode": {"queries": 0, "files": 0, "candidates": 0},
             "web": {"queries": 0, "pages": 0, "candidates": 0},
             "gitee": {"queries": 0, "repos": 0, "candidates": 0},
             "seeds": {"repos": 0, "lists": 0, "candidates": 0},
             "manifests": {"lists": 0, "candidates": 0}}

    print("==== 每日全网源发现（自动搜索，无需人工维护清单） ====")
    cand = {}

    def add_urls(urls, channel):
        for u in urls:
            u = normalize_cand(u)
            if not u.startswith(("http://", "https://")):
                continue
            if _is_own_candidate(u):
                continue  # own 组源单独维护，不进 auto 清单
            if norm(u) not in cand:
                cand[u] = channel

    add_urls(chan_seeds(token, stats), "seeds")
    # ghcode（GitHub 代码搜索）在 Actions 上必撞 429 限流，退避等待把整轮拖到 ~18 分钟，
    # 且实际贡献恒为 0（源都来自 github/web/seeds），故停用。需要时取消下一行注释即可恢复。
    # add_urls(chan_gh_code(token, stats), "ghcode")
    add_urls(chan_web(stats), "web")
    add_urls(chan_github(token, stats), "github")
    add_urls(chan_gitee(stats), "gitee")
    if extra_manifests:
        add_urls(chan_manifests(extra_manifests, stats), "manifests")

    stats["candidates_total"] = len(cand)
    print("[DISC] 四通道候选地址共 %d 个" % len(cand))

    autos = _load(AUTO_SOURCES_FILE)
    autol = _load(AUTO_LIVES_FILE)

    # 存量健康检查 + 自动淘汰
    alive_c = health_check(autos["sources"])
    alive_l = health_check(autol["sources"])
    before_c, before_l = len(autos["sources"]), len(autol["sources"])
    autos["sources"] = [e for e in autos["sources"] if e.get("fail_streak", 0) < FAIL_LIMIT]
    autol["sources"] = [e for e in autol["sources"] if e.get("fail_streak", 0) < FAIL_LIMIT]
    pruned = (before_c - len(autos["sources"])) + (before_l - len(autol["sources"]))
    print("[DISC] 存量健康检查：点播 %d/%d 存活，直播 %d/%d 存活，淘汰 %d 个" % (
        alive_c, before_c, alive_l, before_l, pruned))

    # 过滤已知，按通道优先级取前 MAX_PROBE 个探测
    known = collect_known()
    for e in autos["sources"] + autol["sources"]:
        for u in e.get("urls", []) or []:
            known.add(norm(u))
    order = {"seeds": 0, "github": 1, "web": 2, "gitee": 3, "manifests": 4}
    fresh = sorted(cand.items(), key=lambda kv: order.get(kv[1], 9))
    fresh = [u for u, _c in fresh if norm(u) not in known][:MAX_PROBE]
    print("[DISC] 新候选 %d 个（本轮探测上限 %d）" % (len(fresh), MAX_PROBE))

    def probe_task(u):
        return (u, probe_url(u))

    found = []
    B = 20
    for i in range(0, len(fresh), B):
        if _over_budget():
            print("[DISC] 超时间预算，停止剩余新候选探测")
            break
        with ThreadPoolExecutor(max_workers=12) as ex:
            found += [x for x in ex.map(probe_task, fresh[i:i + B]) if x[1]]

    conf_new = [(u, k, n, fp) for u, (k, n, fp) in found if k in ("single", "multi")]
    live_new = [(u, k, n, fp) for u, (k, n, fp) in found if k == "live"]

    def _url_rank(u):
        # 直连 raw 优先保留，镜像前缀的同类内容去重时舍弃
        if u.startswith("https://raw.githubusercontent.com/"):
            return 0
        if u.startswith("http"):
            return 1
        return 2

    # 内容指纹去重：同一仓库的 ghfast/jsdelivr/ghproxy 镜像与直连同内容，只保留直连
    conf_new.sort(key=lambda x: (-x[2], _url_rank(x[0])))
    live_new.sort(key=lambda x: (-x[2], _url_rank(x[0])))
    seen_fp = set()
    conf_new = [x for x in conf_new if not (x[3] in seen_fp or seen_fp.add(x[3]))]
    seen_fp = set()
    live_new = [x for x in live_new if not (x[3] in seen_fp or seen_fp.add(x[3]))]

    used_ids = {e.get("id") for e in autos["sources"] + autol["sources"]} \
        | _ids_from(os.path.join(CONFIG_DIR, "sources.json")) \
        | _ids_from(os.path.join(CONFIG_DIR, "live_sources.json"))
    added_c, added_l = [], []
    for u, k, n, fp in conf_new:
        if len(added_c) >= max_new_cfg:
            break
        e = make_entry(u, k, n, cand.get(u, "web"))
        if e["id"] in used_ids:
            continue
        used_ids.add(e["id"])
        added_c.append(e)
    for u, k, n, fp in live_new:
        if len(added_l) >= max_new_live:
            break
        e = make_entry(u, k, n, cand.get(u, "web"))
        if e["id"] in used_ids:
            continue
        used_ids.add(e["id"])
        added_l.append(e)

    for e in added_c:
        print("[DISC] + 点播源 %s（%d 站点，via %s）" % (e["name"], e["score"], e["channel"]))
    for e in added_l:
        print("[DISC] + 直播源 %s（%d 频道，via %s）" % (e["name"], e["score"], e["channel"]))
    if not added_c and not added_l:
        print("[DISC] 本轮无新源纳入（候选均已知或未通过校验）")

    autos["sources"] = sorted(autos["sources"] + added_c,
                              key=lambda e: (e.get("fail_streak", 0), -e.get("score", 0)))[:CAP_CONFIG]
    autol["sources"] = sorted(autol["sources"] + added_l,
                              key=lambda e: (e.get("fail_streak", 0), -e.get("score", 0)))[:CAP_LIVE]
    note = "自动全网搜索发现并维护（scripts/discover.py）：新源自动纳入、失效源自动淘汰，无需人工维护。"
    autos.update({"updated_at": now_str(), "note": note})
    autol.update({"updated_at": now_str(), "note": note})
    _save(AUTO_SOURCES_FILE, autos)
    _save(AUTO_LIVES_FILE, autol)

    stats.update({"probed": len(fresh), "added_configs": len(added_c),
                  "added_lives": len(added_l), "pruned": pruned,
                  "auto_configs": len(autos["sources"]),
                  "auto_lives": len(autol["sources"]),
                  "elapsed_sec": round(time.time() - T0, 1)})
    with open(os.path.join(OUTPUT_DIR, "discovery_report.json"), "w", encoding="utf-8") as f:
        json.dump({"date": now_str(), "channels": stats}, f, ensure_ascii=False, indent=1)
    print("[DISC] 完成，用时 %.1fs；报告 output/discovery_report.json" % (time.time() - T0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
