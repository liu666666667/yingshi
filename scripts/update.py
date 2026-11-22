#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yingshi 每日采集更新脚本（纯标准库实现，本地 / GitHub Actions 通用）
=================================================================
流程：
  0. 发现阶段由 scripts/discover.py 独立完成：自动全网搜索新源（GitHub/网页搜索/Gitee），
     探活后写入 auto_sources.json / auto_lives.json 自生长清单，本脚本直接纳入
  1. 读取 config/sources.json（点播主源）+ config/auto_sources.json（自动发现源）
     + config/live_sources.json（直播源）+ config/rules.json（公开过滤规则）
     + config/sensitive.json（可选，本地私有扩展配置）
  2. 并发抓取点播源：单仓直接校验；多仓展开子仓为独立线路
  3. 相对路径重写：spider/ext/jar 的相对路径全部改写为以"原源 URL"为基的绝对地址，
     避免线路存入本仓库后 jar/ext 404 导致整仓解析失败
  4. 内容治理：广告/博彩/垃圾站点屏蔽；特定分类站点自动分离到独立产物（公开产物零包含，
     导航页与 README 不出现任何相关字样/入口）
  5. 生成公开产物：
       output/duocang.json   多仓（收录全部线路）
       output/merged.json    合并单仓（可合并站点合成一个）
       output/live.m3u/.txt  直播源订阅
  6. 生成分类产物：output/private.json（独立文件，不进任何公开入口）
  7. 生成 output/status.json 状态，index.html 前端动态渲染

用法：
  python scripts/update.py                      # 公开采集
  python scripts/update.py --with-sensitive     # 追加本地私有源
  python scripts/update.py --repo USER/yingshi  # 指定仓库生成链接
  python scripts/update.py --base URL           # 指定产物 URL 前缀（测试隧道）
  python scripts/update.py --skip-live           # 跳过直播采集（调试）
  python scripts/update.py --skip-discovery      # 跳过源发现（调试）
"""
import argparse
import datetime
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
OUTPUT_DIR = os.path.join(ROOT, "output")
RAW_DIR = os.path.join(ROOT, "raw")
LINES_DIR = os.path.join(OUTPUT_DIR, "lines")
AUTO_SOURCES_FILE = os.path.join(CONFIG_DIR, "auto_sources.json")

TZ = datetime.timezone(datetime.timedelta(hours=8))

# ---- 网络层 ----
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0"
# 影视仓 App 真实 UA：部分接口对浏览器 UA 返回伪装图片/HTML，对 App UA 才返回真 JSON
APP_UAS = [
    ("Mozilla/5.0", ""),
    ("okhttp/3.15", "com.iptvbox"),
    ("TVBox/1.0.0", "com.iptvbox"),
]
DEFAULT_TIMEOUT = 15
MAX_BYTES = 12 * 1024 * 1024  # 单文件下载上限
TVBOX_KEYS = ("sites", "spiders", "lives", "parses", "flags", "exts")


def mask_own(text):
    """展示名脱敏：产物/页面/状态表里不显示仓库所有者账号。"""
    return str(text or "").replace("liu666666667", "***")


def now_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def idn_encode(url):
    """URL 规范化：IDN 域名编码 + 非字母数字路径百分号编码（中文路径直接请求会崩溃）。"""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        enc = host.encode("idna").decode("ascii")
        path = urllib.parse.quote(p.path or "", safe="/%")
        query = urllib.parse.quote(p.query or "", safe="=&?/%+~")
        netloc = enc + ((":" + str(p.port)) if p.port else "")
        return urllib.parse.urlunsplit((p.scheme, netloc, path, query, p.fragment))
    except Exception:
        return url


def http_get(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_BYTES, ua=None, xrw=None):
    """GET 并返回文本。urllib + 放宽证书校验。可指定 UA（默认 Mozilla/5.0）。
    返回 (文本, 是否像文本)。二进制伪装响应（图片防盗）返回 (None, False)。"""
    target = idn_encode(url)
    headers = {
        "User-Agent": ua or UA,
        "Accept": "application/json,text/plain,*/*",
    }
    if xrw:
        headers["X-Requested-With"] = xrw
    req = urllib.request.Request(target, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response too large")
    # 伪装图片/二进制响应检测：JPEG/PNG/GIF/BMP 魔数或高密度控制字符
    head = data[:16]
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" \
       or head[:6] in (b"GIF89a", b"GIF87a") or head[:2] in (b"BM", b"\x00\x10") \
       or (len(data) and data[:1] == b"\x00"):
        return None, False
    # 官网页面响应检测（浏览器 UA 被导向 HTML 官网）：换 App UA 重拿
    low = data[:256].lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html", b"<head")):
        return None, False
    text = data.decode("utf-8", "ignore")
    if text.count("\x00") > 3 or (len(text) and "\ufffd" in text[:200] and text.count("\ufffd") > len(text) * 0.3):
        return None, False
    return text, True


def fetch_text_with_fallback(url_list, retries=1, github_only_mirror=True, app_ua=False):
    """依次尝试 url_list；同地址失败重试 retries 次；github 直连失败自动加镜像前缀重试。
    app_ua=True 时启用 App UA 轮换（对付伪装图片反爬）。
    返回 (原始url, 实际使用url, 文本)。保证每个地址的总尝试时间可控。"""
    mirrors = ["https://gh-proxy.com/", "https://ghproxy.net/"]
    ua_list = APP_UAS if app_ua else [(UA, "")]
    last_err = None
    for url in url_list:
        t = 12 if len(url_list) > 1 else DEFAULT_TIMEOUT
        attempts = [url]
        if github_only_mirror and "githubusercontent.com" in url:
            attempts += [m + url for m in mirrors]
        for u in attempts:
            for ua, xrw in ua_list:
                for attempt in range(1, retries + 2):
                    try:
                        text, is_text = http_get(u, timeout=t, ua=ua, xrw=xrw)
                        if is_text:
                            return url, u, text
                        # 二进制伪装响应：不重试同 UA，直接换下一个 UA
                        last_err = ValueError("binary-disguise response")
                        break
                    except Exception as e:
                        last_err = e
                        if attempt <= retries:
                            time.sleep(1)
                        continue
    raise last_err or RuntimeError("no urls")


def parse_json_lenient(text):
    text = text.lstrip("\ufeff").strip()
    text = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("//"))
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        d = json.JSONDecoder(strict=False)
        obj, _ = d.raw_decode(text)
        return obj


def is_tvbox_config(data):
    if not isinstance(data, dict):
        return False
    if any(k in data for k in TVBOX_KEYS):
        return True
    if isinstance(data.get("urls"), list) and data["urls"]:
        return True
    return False


def is_multi_warehouse(data):
    return isinstance(data, dict) and isinstance(data.get("urls"), list) and data["urls"] and not data.get("sites")


# ---- 相对路径重写（核心：修复 jar/ext 404 导致的解析失败） ----
def _join_base(base_url, ref):
    """把相对引用 ref 拼到 base_url 的目录上，返回绝对 URL。保留 ;md5;xxx 后缀。"""
    if not ref or not isinstance(ref, str):
        return ref
    ref = ref.strip()
    if ref.startswith(("http://", "https://", "{", "[", "data:", "clan://", "file://")):
        return ref
    core, sep, tail = ref.partition(";")
    if not core:
        return ref
    if core.startswith("/"):
        p = urllib.parse.urlsplit(base_url)
        abs_core = "%s://%s%s" % (p.scheme, p.netloc, core)
    else:
        base_dir = base_url.rsplit("/", 1)[0]
        abs_core = base_dir + "/" + core.lstrip("./")
    return abs_core + sep + tail


def absolutize(data, base_url):
    """把配置里 spider / sites[].ext（含 dict 内 jar）的相对路径重写为以 base_url 为基的绝对地址。"""
    if not isinstance(data, dict) or not base_url:
        return data

    def fix_ext(ext):
        if isinstance(ext, str):
            return _join_base(base_url, ext)
        if isinstance(ext, dict):
            for k in ("jar", "api", "url"):
                if k in ext and isinstance(ext[k], str):
                    ext[k] = _join_base(base_url, ext[k])
        return ext

    spider = data.get("spider")
    if isinstance(spider, str) and spider and not spider.startswith(("http", "{", "[")):
        data["spider"] = _join_base(base_url, spider)
    for s in data.get("sites", []) or []:
        if isinstance(s, dict):
            if "ext" in s:
                s["ext"] = fix_ext(s["ext"])
            if isinstance(s.get("api"), str) and s["api"].endswith((".js", ".jar", ".py")) \
               and not s["api"].startswith(("http", "{")):
                s["api"] = _join_base(base_url, s["api"])
    return data


# ---- 过滤器 ----
# 分类分拣词表：命中站点进入独立产物（不进入任何公开入口/页面/README）
# 宽口径 + 白名单先放行（webdav/dav），与主流同类项目对齐：裸 av/福利/涩/成人 等均命中
SEPARATE_KEYWORDS = [
    "成人", "成人影院", "色情", "情色", "大尺度", "擦边", "涩", "福利", "福利姬",
    "里番", "伦理", "午夜场", "十八禁", "18禁", "18+", "🔞",
    "亚洲av", "av影院", "av在线", "av", "黄色资源", "啪啪", "污污",
    "麻豆", "天美传媒", "精东", "九一制片", "91制片", "91porn",
    "fc2", "无码", "有码", "吃瓜网", "爆料",
    "4kav", "4k-av", "4k_av", "porn", "nsfw", "sex", "xxx", "jav", "裸聊",
]
# 短英文词用边界匹配（避免 huavod/Xxxg 这类子串误伤；编译一次）
SEP_BARE_RX = {
    "av": re.compile(r"(?<![a-z])av(?![a-z])"),
    "xxx": re.compile(r"(?<![a-z])xxx(?![a-z])"),
    "sex": re.compile(r"(?<![a-z])sex(?![a-z])"),
    "porn": re.compile(r"(?<![a-z])porn(?![a-z])"),
    "nsfw": re.compile(r"(?<![a-z])nsfw(?![a-z])"),
    "jav": re.compile(r"(?<![a-z])jav(?![a-z])"),
}
# 分拣白名单：命中先放行（裸 av 会误伤 WebDAV 功能站，先过白名单再匹配宽词表）
SEPARATE_ALLOW = ["webdav", "dav", "豆瓣", "douban"]
# 源级归集词：源名/URL 命中则整源归入独立产物（不进任何公开入口）
SOURCE_ADULT_KEYWORDS = [
    "nsfw", "adult", "porn", "xxx", "色情", "成人", "18禁", "18+", "av在线",
]


class Filter:
    def __init__(self, rules_path, sensitive_path=None):
        with open(rules_path, encoding="utf-8") as f:
            self.rules = json.load(f)
        self.sensitive = None
        self.sensitive_enabled = False
        if sensitive_path and os.path.exists(sensitive_path):
            with open(sensitive_path, encoding="utf-8") as f:
                self.sensitive = json.load(f)
            self.sensitive_enabled = bool(self.sensitive.get("enabled", True))

        sf = self.rules.get("site_filters", {})
        self.block_kw = [w for w in sf.get("block_keywords", []) if w]
        self.block_domains = [d.lower() for d in sf.get("block_domains", []) if d]
        self.block_regex = [re.compile(p, re.I) for p in sf.get("block_regex", []) if p]
        self.wl_domains = [d.lower() for d in sf.get("whitelist_domains", []) if d]
        self.wl_names = sf.get("whitelist_name_contains", [])

        lf = self.rules.get("live_filters", {})
        self.live_block_group = lf.get("block_group_keywords", [])
        self.live_block_ch = lf.get("block_channel_keywords", [])
        self.live_wl_ch = lf.get("whitelist_channel_keywords", [])

        # 分拣词（内置 + 本地敏感配置扩展）
        self.sep_kw = [w for w in SEPARATE_KEYWORDS if w]
        if self.sensitive_enabled:
            self.sep_kw += [w for w in self.sensitive.get("isolate_keywords", []) if w]
        self.sep_allow = [w.lower() for w in SEPARATE_ALLOW if w]
        if self.sensitive_enabled:
            self.sep_allow += [w.lower() for w in self.sensitive.get("isolate_allow_keywords", []) if w]
        self.src_sep_kw = [w for w in SOURCE_ADULT_KEYWORDS if w]
        self.iso_kw_local = []
        self.iso_rx_local = []
        if self.sensitive_enabled:
            self.iso_kw_local = [w for w in self.sensitive.get("isolate_keywords", []) if w]
            self.iso_rx_local = [re.compile(p, re.I) for p in self.sensitive.get("isolate_regex", []) if p]

    def check_separate(self, site):
        """站点级分拣：扫 name+key+api 三字段（大小写不敏感），白名单先放行。
        短英文词用边界匹配，避免 huavod/Xxxg 等子串误伤。"""
        if not isinstance(site, dict):
            return None
        txt = ("%s %s %s" % (site.get("name", ""), site.get("key", ""),
                             site.get("api", ""))).lower()
        for a in self.sep_allow:
            if a and a in txt:
                return None
        for w in self.sep_kw:
            if not w:
                continue
            lw = w.lower()
            rx = SEP_BARE_RX.get(lw)
            if rx is not None:
                if rx.search(txt):
                    return w
            elif lw in txt:
                return w
        return None

    def check_source_separate(self, src_name, src_url=""):
        """源级归集：源名/URL 命中则整源归入独立产物。返回命中词或 None。"""
        hay = ("%s %s" % (src_name or "", src_url or "")).lower()
        for a in self.sep_allow:
            if a and a in hay:
                return None
        for w in self.src_sep_kw:
            if w and w in hay:
                return w
        return None

    # 站点级别：返回 (action, hit) action ∈ keep / block / isolate
    def check_site(self, site):
        if not isinstance(site, dict):
            return "block", "not-dict"
        name = str(site.get("name", ""))
        api = str(site.get("api", "")) + " " + str(site.get("ext", "") or "")
        for wn in self.wl_names:
            if wn in name:
                return "keep", None
        try:
            api_host = (urllib.parse.urlsplit(str(site.get("api", ""))).hostname or "").lower()
        except Exception:
            api_host = ""
        for d in self.wl_domains:
            if d in api_host:
                return "keep", None
        for d in self.block_domains:
            if d and (d in api_host or d in api.lower()):
                return "block", "domain:" + d
        for w in self.block_kw:
            if w and (w in name or w in api):
                return "block", "kw:" + w
        for rx in self.block_regex:
            if rx.search(name) or rx.search(api):
                return "block", "rx:" + rx.pattern[:30]
        # 私有分流（仅本地敏感配置启用时生效，进 output/private/ 目录）
        if self.sensitive_enabled:
            for w in self.iso_kw_local:
                if w and (w in name or w in api):
                    return "isolate", w
            for rx in self.iso_rx_local:
                if rx.search(name) or rx.search(api):
                    return "isolate", "rx"
        return "keep", None


# ---- 点播采集 ----
def clean_single_config(data, flt):
    """清洗单仓：sites 分 keep / separate / isolate 三组；其余字段保留。"""
    sites_keep, sites_sep, sites_iso = [], [], []
    blocked = 0
    for s in data.get("sites", []) or []:
        action, hit = flt.check_site(s)
        if action == "keep":
            if flt.check_separate(s):
                sites_sep.append(s)
            else:
                sites_keep.append(s)
        elif action == "isolate":
            sites_iso.append(s)
        else:
            blocked += 1
    c_pub = {k: v for k, v in data.items() if k != "sites"}
    c_pub["sites"] = sites_keep
    c_sep = {k: v for k, v in data.items() if k != "sites"}
    c_sep["sites"] = sites_sep
    c_iso = None
    if sites_iso and flt.sensitive_enabled:
        c_iso = {k: v for k, v in data.items() if k != "sites"}
        c_iso["sites"] = sites_iso
    return c_pub, c_sep, c_iso, blocked


def merge_single_configs(items, cap=1500):
    """items: [(fn,name,config)] -> [单仓1, 单仓2, ...] 多卷列表。

    站点按源轮询合并；填满一份（cap 个）后另起一卷新文件（对应 merged2.json 等），
    绝不因超限丢弃站点。所有卷共享同一份去重后的 lives/parses 等附加列表。"""
    if not items:
        return []
    _sid, _base_name, base = items[0]

    def new_part(idx):
        m = {}
        for k, v in base.items():
            if k not in ("sites", "lives", "parses", "doh", "rules", "flags", "exts"):
                m[k] = v
        suffix = "" if idx == 1 else "（第%d卷）" % idx
        m["warningText"] = ("由 yingshi 聚合 %d 条线路自动合并生成%s，仅供学习交流"
                            % (len(items), suffix))
        m["sites"] = []
        return m

    parts = [new_part(1)]
    queues = [list(data.get("sites", []) or []) for _s, _n, data in items]
    seen = set()
    total = 0
    progressed = True
    while progressed:
        progressed = False
        for q in queues:
            if not q:
                continue
            progressed = True
            s = q.pop(0)
            if not isinstance(s, dict):
                continue
            # 非标准站点剔除：{"name","url"} 型公共说明条目等（无 api/ext 无法解析，
            # 会造成影视仓导入合成仓解析失败）
            if not s.get("name") or not (s.get("api") or s.get("ext")):
                continue
            key = str(s.get("key") or "")
            if not key:
                # 强制补 key：影视仓要求站点 key 唯一非空
                key = "s%d" % total
                s = dict(s)
                s["key"] = key
            nk = key
            i = 0
            while nk in seen and i < 200:
                i += 1
                nk = "%s_%s_%d" % (_sid[:12], key[:20], i)
            if nk in seen:
                continue
            seen.add(nk)
            if nk != key:
                s = dict(s)
                s["key"] = nk
            if len(parts[-1]["sites"]) >= cap:
                parts.append(new_part(len(parts) + 1))
            parts[-1]["sites"].append(s)
            total += 1

    def merge_list(field):
        mseen, out = set(), []
        for _sid, _n, data in items:
            for it in data.get(field, []) or []:
                if isinstance(it, dict):
                    ident = str(it.get("name") or it.get("url") or it)
                else:
                    ident = str(it)
                if ident in mseen:
                    continue
                mseen.add(ident)
                out.append(it)
        return out

    for f in ("lives", "parses", "doh", "rules", "flags", "exts"):
        vals = merge_list(f)
        for m in parts:
            m[f] = vals
    return parts


class VodCollector:
    def __init__(self, cfg, flt, status):
        self.cfg, self.flt, self.status = cfg, flt, status
        # 三组分桶：own=自己的项目仓库 / manual=手动维护清单 / auto=网络自动发现
        self.public = {"auto": [], "manual": [], "own": []}
        self.separate, self.private = [], []
        self.counter = 0

    def _safe_fn(self, sid):
        self.counter += 1
        safe = re.sub(r"[^0-9A-Za-z_-]", "", sid)[:20]
        return "l%03d_%s.json" % (self.counter, safe or "line")

    def absorb(self, sid, name, data, base_url, group="auto"):
        """base_url：该配置实际抓取成功的地址，用于 absolutize。
        group ∈ auto / manual / own（own 组单独成产物，页面零展示）。
        源级命中分拣词时整源归入独立产物；否则站点级逐个分拣。"""
        data = absolutize(data, base_url)
        src_hit = self.flt.check_source_separate(name, base_url or "")
        if src_hit:
            # 整源归集：全部站点进独立产物，公开不收
            pub_cfg = {k: v for k, v in data.items() if k != "sites"}
            pub_cfg["sites"] = []
            sep_cfg, iso_cfg, blocked = data, None, 0
        else:
            pub_cfg, sep_cfg, iso_cfg, blocked = clean_single_config(data, self.flt)
        fn = self._safe_fn(sid)
        self.status.setdefault("blocked", []).append({"source": sid, "blocked": blocked})
        dname = mask_own(name)
        if pub_cfg.get("sites"):
            self.public.setdefault(group, []).append((fn, name, pub_cfg))
        if sep_cfg.get("sites"):
            self.separate.append((fn.replace(".json", "_s.json"), dname, sep_cfg))
        if iso_cfg and iso_cfg.get("sites"):
            self.private.append((fn.replace(".json", "_p.json"), dname + "(分流)", iso_cfg))
        print("       [%s] %-36s 公开%d 分拣%d 屏蔽%d%s" % (
            "OK", dname[:36], len(pub_cfg.get("sites", [])),
            len(sep_cfg.get("sites", [])), blocked,
            ("（整源归集：%s）" % src_hit) if src_hit else ""))

    def run(self, sources, manual_ids=None):
        """manual_ids：手动维护清单的源 id 集合（用于分组；
        URL 命中自己仓库特征的自动归 own 组）。"""
        manual_ids = set(manual_ids or ())
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(self.fetch_one, sources))
        for r in results:
            src = r["src"]
            urls_join = " ".join(str(u) for u in src.get("urls", []))
            group = "own" if ("liu666666667/yingshicang" in urls_join
                              or "liu666666667/yingshi/" in urls_join) \
                else ("manual" if src["id"] in manual_ids else "auto")
            if src.get("type") == "multi" and src.get("expand"):
                if not r["ok"]:
                    print("[FAIL] %s : %s" % (src["id"], r["err"]))
                    if group != "own":
                        self.status["sources"].append(
                            {"id": src["id"], "name": mask_own(src.get("name")), "ok": False, "err": r["err"]})
                    continue
                for nm, d, bu in r["children"]:
                    self.absorb(src["id"], "%s-%s" % (src.get("name", src["id"]), nm), d, bu, group)
                print("[OK]   %s（展开子仓 %d 条，组=%s）" % (src["id"], len(r["children"]), group))
                if group != "own":
                    self.status["sources"].append(
                        {"id": src["id"], "name": mask_own(src.get("name")), "ok": True,
                         "lines": len(r["children"])})
            else:
                if not r["ok"]:
                    print("[FAIL] %s : %s" % (src["id"], r["err"]))
                    if group != "own":
                        self.status["sources"].append(
                            {"id": src["id"], "name": mask_own(src.get("name")), "ok": False, "err": r["err"]})
                    continue
                self.absorb(src["id"], src.get("name", src["id"]), r["data"], r["base_url"], group)
                if group != "own":
                    self.status["sources"].append(
                        {"id": src["id"], "name": mask_own(src.get("name")), "ok": True, "lines": 1})
                print("[OK]   %s（组=%s）" % (src["id"], group))

    def fetch_one(self, src):
        sid = src["id"]
        app_ua = bool(src.get("app_ua", True))  # 点播接口默认启用 App UA 轮换
        if src.get("type") == "multi" and src.get("expand"):
            try:
                _o, _u, text = fetch_text_with_fallback(src["urls"], app_ua=app_ua)
                data = parse_json_lenient(text)
                if not is_multi_warehouse(data):
                    raise ValueError("not multi")
                sub = []
                for it in data["urls"][:60]:
                    if isinstance(it, dict) and it.get("url"):
                        sub.append((str(it.get("name") or "子仓"), str(it["url"])))
                with ThreadPoolExecutor(max_workers=6) as ex:
                    got = list(ex.map(self._child, sub))
                children = [g for g in got if g]
                return {"src": src, "ok": bool(children), "children": children, "err": None}
            except Exception as e:
                return {"src": src, "ok": False, "children": [], "err": str(e)[:80]}
        try:
            orig, used, text = fetch_text_with_fallback(src["urls"], app_ua=app_ua)
            data = parse_json_lenient(text)
            if not is_tvbox_config(data):
                raise ValueError("not tvbox config")
            return {"src": src, "ok": True, "data": data, "base_url": used, "err": None}
        except Exception as e:
            return {"src": src, "ok": False, "data": None, "err": str(e)[:80]}

    def _child(self, p):
        nm, url = p
        try:
            _o, used, text = fetch_text_with_fallback([url], retries=0, app_ua=True)
            d = parse_json_lenient(text)
            if is_tvbox_config(d) and not is_multi_warehouse(d):
                return (nm, d, used)
        except Exception:
            pass
        return None

    def collect_private_sources(self):
        """处理 sensitive.json 里的整源私有列表（只在本地且开关打开时）。"""
        if not (self.flt.sensitive and self.flt.sensitive_enabled):
            return
        for src in self.flt.sensitive.get("sources", []):
            try:
                _o, used, text = fetch_text_with_fallback(src.get("urls", []))
                d = parse_json_lenient(text)
                if is_tvbox_config(d):
                    fn = self._safe_fn("priv_" + src["id"])
                    d = absolutize(d, used)
                    self.private.append((fn.replace(".json", "_p.json"),
                                         src.get("name", src["id"]), d))
                    print("[PRIVATE-OK] %s" % src["id"])
            except Exception as e:
                print("[PRIVATE-FAIL] %s : %s" % (src["id"], str(e)[:60]))


# ---- 自动发现源（由 scripts/discover.py 每日自动全网搜索维护） ----
# discover.py 会自动在 GitHub / 网页搜索引擎 / Gitee 等渠道搜索新源，探活后写入
# config/auto_sources.json（点播）与 config/auto_lives.json（直播），并对存量源做
# 健康检查、连续失效自动淘汰，全程无需人工维护清单。update.py 只负责读取这两个清单。


def load_auto_sources():
    if os.path.exists(AUTO_SOURCES_FILE):
        try:
            with open(AUTO_SOURCES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sources": []}



def parse_m3u(text):
    out = []
    group, name = "未分组", ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                group = m.group(1).strip() or "未分组"
            name = line.split(",", 1)[-1].strip()
        elif line.startswith("#"):
            continue
        elif "://" in line:
            out.append((group, name or "未命名", line))
    return out


def parse_txt_live(text):
    out = []
    group = "未分组"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#genre#" in line:
            group = line.split(",")[0].strip() or "未分组"
            continue
        if "://" in line and "," in line:
            nm, u = line.split(",", 1)
            out.append((group, nm.strip(), u.strip()))
    return out


def collect_live(cfg, flt, status):
    sources = cfg.get("sources", [])
    lock_stats = status.setdefault("live_sources", [])

    def fetch_one(src):
        try:
            _o, _u, text = fetch_text_with_fallback(src["urls"])
            if "#EXTINF" in text or "#EXTM3U" in text:
                ch = parse_m3u(text)
            else:
                ch = parse_txt_live(text)
            if not ch:
                raise ValueError("empty")
            return {"src": src, "ok": True, "channels": ch, "err": None}
        except Exception as e:
            return {"src": src, "ok": False, "channels": [], "err": str(e)[:80]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch_one, sources))

    url_seen = set()
    name_counts = {}
    grouped = {}
    total_in, kept = 0, 0
    for r in results:
        src = r["src"]
        if r["ok"]:
            print("[LIVE-OK]  %s (%d 频道)" % (src["id"], len(r["channels"])))
        else:
            print("[LIVE-FAIL] %s : %s" % (src["id"], r["err"]))
        lock_stats.append({
            "id": src["id"], "name": mask_own(src.get("name")), "ok": r["ok"],
            "channels": len(r["channels"]), "err": r["err"]})
        if not r["ok"]:
            continue
        for group, name, url in r["channels"]:
            total_in += 1
            if any(k in group for k in flt.live_block_group):
                continue
            base_name = re.sub(r"\s+", "", name)
            if not any(k in base_name.upper() for k in
                       [x.upper() for x in flt.live_wl_ch]) and \
               any(k in base_name for k in flt.live_block_ch):
                continue
            if url in url_seen:
                continue
            url_seen.add(url)
            n = name_counts.get(base_name, 0)
            if n >= 3:
                continue
            name_counts[base_name] = n + 1
            grouped.setdefault(group, []).append((name, url))
            kept += 1

    def group_key(g):
        order = ["央视", "CCTV", "卫视", "地方", "港台", "国际"]
        for i, k in enumerate(order):
            if k in g:
                return (i, g)
        return (len(order), g)

    lines = ["#EXTM3U"]
    txt_lines = []
    for g in sorted(grouped.keys(), key=group_key):
        chs = grouped[g]
        txt_lines.append("%s,#genre#" % g)
        for name, url in chs:
            safe_g = g.replace('"', "")
            safe_n = name.replace('"', "")
            lines.append('#EXTINF:-1 group-title="%s",%s' % (safe_g, safe_n))
            lines.append(url)
            txt_lines.append("%s,%s" % (name, url))
    return lines, txt_lines, total_in, kept, len(grouped)


# ---- 产物生成 ----
def build_links(repo, path):
    repo = repo or "YOUR_NAME/yingshi"
    user = repo.split("/")[0]
    page = "https://%s.github.io/%s/%s" % (user, repo.split("/")[-1], path)
    raw = "https://raw.githubusercontent.com/%s/main/%s" % (repo, path)
    acc = "https://gh-proxy.com/" + raw
    return page, raw, acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-sensitive", action="store_true", help="启用本地私有配置")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or os.environ.get("REPO"))
    ap.add_argument("--base", help="自定义产物 URL 前缀（测试隧道/自建域名），优先于 --repo")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-discovery", action="store_true", help="跳过纳入自动发现源（调试）")
    args = ap.parse_args()

    t0 = time.time()
    for d in (LINES_DIR, RAW_DIR):
        os.makedirs(d, exist_ok=True)
    # 清空上轮线路文件（失效源的旧线路不再残留），duocang 每轮全量重写
    for fn in os.listdir(LINES_DIR):
        os.remove(os.path.join(LINES_DIR, fn))
    # 清空上轮合并/分拣/分组产物（防止分卷/分组数量变化时残留旧文件）
    for fn in os.listdir(OUTPUT_DIR):
        if re.match(r"merged\d*\.json$", fn) or re.match(r"private\d*\.json$", fn) \
           or fn in ("merged_all.json", "manual.json", "own.json"):
            os.remove(os.path.join(OUTPUT_DIR, fn))
    # own 分线路目录同样重建
    o_dir = os.path.join(OUTPUT_DIR, "lines_own")
    if os.path.isdir(o_dir):
        for fn in os.listdir(o_dir):
            os.remove(os.path.join(o_dir, fn))

    with open(os.path.join(CONFIG_DIR, "sources.json"), encoding="utf-8") as f:
        vod_cfg = json.load(f)
    with open(os.path.join(CONFIG_DIR, "live_sources.json"), encoding="utf-8") as f:
        live_cfg = json.load(f)
    flt = Filter(os.path.join(CONFIG_DIR, "rules.json"),
                 os.path.join(CONFIG_DIR, "sensitive.json") if args.with_sensitive else None)

    status = {"updated_at": now_str(), "repo": args.repo or "(local)",
              "sources": [], "live_sources": []}

    # 0. 三组源：manual（config/sources.json）/ own（config/own_sources.json，自己维护）
    #    / auto（discover.py 自生长清单）。own 的站点参与最终合并去重，但不进公开产物。
    manual_sources = list(vod_cfg.get("sources", []))
    manual_ids = {s["id"] for s in manual_sources}
    own_sources = []
    own_path = os.path.join(CONFIG_DIR, "own_sources.json")
    if os.path.exists(own_path):
        try:
            with open(own_path, encoding="utf-8") as f:
                own_sources = json.load(f).get("sources", []) or []
        except Exception as e:
            print("[OWN] own_sources.json 读取失败：%s" % str(e)[:50])
    all_sources = manual_sources + own_sources
    existing_ids = {s["id"] for s in all_sources}
    n_disc = 0
    if not args.skip_discovery:
        for s in load_auto_sources().get("sources", []):
            if s.get("id") and s["id"] not in existing_ids:
                all_sources.append(s)
                existing_ids.add(s["id"])
                n_disc += 1
    if n_disc:
        print("==== 0/4 纳入自动发现源 %d 个（全网搜索自动维护） ====" % n_disc)

    print("==== 1/4 采集点播源（manual %d + own %d + auto %d） ====" % (
        len(manual_sources), len(own_sources), len(all_sources) - len(manual_sources) - len(own_sources)))
    col = VodCollector(vod_cfg, flt, status)
    col.run(all_sources, manual_ids=manual_ids)
    if flt.sensitive_enabled:
        print("==== 1.5 采集本地私有源 ====")
        col.collect_private_sources()

    print("==== 2/4 生成点播产物 ====")

    def url_for(path):
        if args.base:
            return args.base.rstrip("/") + "/" + path
        if args.repo:
            return build_links(args.repo, path)[0]
        return path

    # 分组产物：auto+manual 进公开多仓；own 单独成产物（页面零展示）
    public_items = col.public["auto"] + col.public["manual"]
    own_items = col.public["own"]

    duocang = {"urls": [], "warningText": "yingshi 多仓聚合，每日自动更新"}
    for fn, name, data in public_items:
        with open(os.path.join(LINES_DIR, fn), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        duocang["urls"].append({"name": name, "url": url_for("output/lines/" + fn)})
    if not args.skip_live:
        duocang["urls"].append({"name": "直播源(生活/影视直播)", "url": url_for("output/live.m3u")})
    # manual 单独多仓（手动维护清单的独立入口）
    if any(g == "manual" for g in ("manual",)) or col.public["manual"]:
        m_dc = {"urls": [], "warningText": "yingshi 手动维护源多仓"}
        for fn, name, data in col.public["manual"]:
            m_dc["urls"].append({"name": name, "url": url_for("output/lines/" + fn)})
        if not args.skip_live:
            m_dc["urls"].append({"name": "直播源(生活/影视直播)", "url": url_for("output/live.m3u")})
        with open(os.path.join(OUTPUT_DIR, "manual.json"), "w", encoding="utf-8") as f:
            json.dump(m_dc, f, ensure_ascii=False, indent=1)
    # own 单独多仓（不进公开入口/页面/README）
    if own_items:
        o_dir = os.path.join(OUTPUT_DIR, "lines_own")
        os.makedirs(o_dir, exist_ok=True)
        o_dc = {"urls": [], "warningText": "yingshi 自维护线路多仓"}
        for fn, name, data in own_items:
            with open(os.path.join(o_dir, fn), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            o_dc["urls"].append({"name": name, "url": url_for("output/lines_own/" + fn)})
        if not args.skip_live:
            o_dc["urls"].append({"name": "直播源(生活/影视直播)", "url": url_for("output/live.m3u")})
        with open(os.path.join(OUTPUT_DIR, "own.json"), "w", encoding="utf-8") as f:
            json.dump(o_dc, f, ensure_ascii=False, indent=1)
        print("[OWN] 自维护线路 %d 条已写入 output/own.json（不进公开入口）" % len(own_items))

    # 最终线路：三源合一去重（own 也参与合并，用户直接用这个）
    final_items = public_items + own_items
    merged_parts = merge_single_configs(final_items)
    merged_sites_total = 0
    for i, m in enumerate(merged_parts, 1):
        fn = "merged.json" if i == 1 else "merged%d.json" % i
        with open(os.path.join(OUTPUT_DIR, fn), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=1)
        merged_sites_total += len(m.get("sites", []))
    # 全量版合并单仓：全部站点集中到一个文件（三源合一的去重结果）
    if merged_parts:
        merged_all = dict(merged_parts[0])
        merged_all["sites"] = [s for m in merged_parts for s in m.get("sites", [])]
        merged_all["warningText"] = ("由 yingshi 聚合 %d 条线路自动合并生成（全量版 %d 站点），仅供学习交流"
                                     % (len(final_items), merged_sites_total))
        with open(os.path.join(OUTPUT_DIR, "merged_all.json"), "w", encoding="utf-8") as f:
            json.dump(merged_all, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUTPUT_DIR, "duocang.json"), "w", encoding="utf-8") as f:
        json.dump(duocang, f, ensure_ascii=False, indent=1)
    print("多仓线路 %d 条（auto+manual）/ 合并单仓 %d 卷共 %d 站点（含 own 去重，全量版 merged_all.json）" % (
        len(duocang["urls"]), len(merged_parts), merged_sites_total))

    # 分类分拣产物（独立文件，不进任何公开入口）
    sep_count = 0
    if col.separate:
        sep_parts = merge_single_configs(col.separate, cap=800)
        for i, m in enumerate(sep_parts, 1):
            fn = "private.json" if i == 1 else "private%d.json" % i
            with open(os.path.join(OUTPUT_DIR, fn), "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False, indent=1)
            sep_count += len(m.get("sites", []))
        print("分拣产物：output/private.json%s（%d 站点，不进公开入口）" % (
            "" if len(sep_parts) == 1 else " 等共%d卷" % len(sep_parts), sep_count))

    # 本地私有产物（整目录，gitignore，仅 --with-sensitive）
    if col.private:
        pdir = os.path.join(OUTPUT_DIR, "private")
        plines = os.path.join(pdir, "lines")
        os.makedirs(plines, exist_ok=True)
        p_duocang = {"urls": [], "warningText": "yingshi 私有产物（仅本地）"}
        for fn, name, data in col.private:
            with open(os.path.join(plines, fn), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            p_duocang["urls"].append({"name": name, "url": "./lines/" + fn})
        with open(os.path.join(pdir, "duocang_private.json"), "w", encoding="utf-8") as f:
            json.dump(p_duocang, f, ensure_ascii=False, indent=1)
        for i, m in enumerate(merge_single_configs(col.private, cap=800), 1):
            fn = "merged_private.json" if i == 1 else "merged_private%d.json" % i
            with open(os.path.join(pdir, fn), "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False, indent=1)
        print("[PRIVATE] 私有线路 %d 条已写入 output/private/" % len(col.private))

    # 自动发现的直播源（自生长清单）
    n_auto_lives = 0
    if not args.skip_discovery:
        alp = os.path.join(CONFIG_DIR, "auto_lives.json")
        if os.path.exists(alp):
            try:
                with open(alp, encoding="utf-8") as f:
                    _al = json.load(f)
                have = {s.get("id") for s in live_cfg.get("sources", [])}
                for s in _al.get("sources", []) or []:
                    if s.get("id") and s["id"] not in have:
                        live_cfg.setdefault("sources", []).append(s)
                        have.add(s["id"])
                        n_auto_lives += 1
            except Exception as e:
                print("[LIVE] 自动直播源读取失败：%s" % str(e)[:50])

    print("==== 3/4 采集直播源 ====")
    if not args.skip_live:
        try:
            lines, txt_lines, total_in, kept, groups = collect_live(live_cfg, flt, status)
            with open(os.path.join(OUTPUT_DIR, "live.m3u"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            with open(os.path.join(OUTPUT_DIR, "live.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(txt_lines) + "\n")
            status["live_stats"] = {"input_channels": total_in, "kept": kept,
                                    "groups": groups}
            print("直播：入库 %d -> 去重保留 %d 频道，%d 分组" % (total_in, kept, groups))
        except Exception as e:
            print("[LIVE] 直播采集异常（不影响点播产物）：%s" % e)

    print("==== 4/4 写状态 ====")
    status["updated_at"] = now_str()
    status["stats"] = {
        "public_lines": len(duocang["urls"]),
        "merged_files": len(merged_parts),
        "merged_sites": merged_sites_total,
        "separate_sites": sep_count,
        "blocked_sites": sum(b.get("blocked", 0) for b in status.get("blocked", [])),
        "discovered_configs": n_disc,
        "discovered_lives": n_auto_lives,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(os.path.join(OUTPUT_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    badge = {"schemaVersion": 1, "label": "yingshi",
             "message": "%d 线路 / 直播 %s" % (
                 status["stats"]["public_lines"],
                 status.get("live_stats", {}).get("kept", "-")),
             "color": "green" if status["stats"]["public_lines"] > 0 else "red"}
    with open(os.path.join(OUTPUT_DIR, "shield.json"), "w", encoding="utf-8") as f:
        json.dump(badge, f, ensure_ascii=False, indent=1)
    print("完成，用时 %.1fs；产物见 output/" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
