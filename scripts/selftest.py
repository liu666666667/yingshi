# -*- coding: utf-8 -*-
"""产物完整性 + 过滤规则单测"""
import json, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("== 1. JSON 全量解析校验 ==")
bad = 0
for base, _, files in os.walk(os.path.join(ROOT, "output")):
    if "private" in base:
        continue
    for fn in files:
        if fn.endswith(".json"):
            p = os.path.join(base, fn)
            try:
                json.load(open(p, encoding="utf-8"))
            except Exception as e:
                bad += 1
                print("  BAD:", fn, e)
print("  坏文件:", bad)

print("== 2. m3u/txt 结构 ==")
ltxt = open(os.path.join(ROOT, "output", "live.txt"), encoding="utf-8").read().splitlines()
print("  live.txt 行数:", len(ltxt), "| 首行:", ltxt[0][:40], "| #genre# 分组数:", sum(1 for l in ltxt if "#genre#" in l))
m3u = [l for l in open(os.path.join(ROOT, "output", "live.m3u"), encoding="utf-8").read().splitlines() if "://" in l]
import re
m3u_names = set()
print("  live.m3u URL行:", len(m3u))
# 直播频道名统计
ch = []
for line in open(os.path.join(ROOT, "output", "live.m3u"), encoding="utf-8"):
    if line.startswith("#EXTINF"):
        ch.append(line.split(",", 1)[-1].strip())
print("  频道数:", len(ch), "| CCTV频道:", sum(1 for c in ch if "CCTV" in c.upper()))

print("== 3. merged.json 结构 ==")
m = json.load(open(os.path.join(ROOT, "output", "merged.json"), encoding="utf-8"))
keys = [s["key"] for s in m["sites"]]
print("  sites:", len(m["sites"]), "| key唯一:", len(set(keys)) == len(keys),
      "| lives:", len(m.get("lives", [])), "| parses:", len(m.get("parses", [])))
print("  前3站点:", [(s["key"], s["name"]) for s in m["sites"][:3]])

print("== 4. 过滤规则单测 ==")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import importlib.util
spec = importlib.util.spec_from_file_location("upd", os.path.join(ROOT, "scripts", "update.py"))
upd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upd)

flt = upd.Filter(os.path.join(ROOT, "config", "rules.json"))
cases = [
    ({"name": "澳门威尼斯人官网", "api": "http://x.com/a.php"}, "block", "博彩关键词+正则"),
    ({"name": "百家乐娱乐城", "api": "http://y.com/b.php"}, "block", "博彩关键词"),
    ({"name": "牛牛游戏大厅", "api": "http://z.com/c.php"}, "block", "棋牌正则"),
    ({"name": "星河彩票", "api": "http://l.com/d.php"}, "block", "彩票关键词"),
    ({"name": "正常影院", "api": "http://api.duboku99.com/api.php"}, "keep", "独播库白名单场景(keep)"),
    ({"name": "牛牛影院", "api": "http://nn.com/vod.php"}, "keep", "纯'牛牛'影视站不误伤"),
    ({"name": "澳门回归电影", "api": "http://mo.com/1.php"}, "keep", "含'澳门'但非博彩"),
    ({"name": "推广付费广告", "api": "http://ad.com/x.php"}, "keep", "点播站名不含规则词则保留"),
]
allok = True
for site, expect, why in cases:
    action, _hit = flt.check_site(site)
    ok = "OK" if action == expect else "!!FAIL"
    if action != expect:
        allok = False
    print("  [%s] %-14s expect=%-7s got=%-7s  %s" % (ok, site["name"], expect, action, why))

print("== 5. 直播过滤单测 ==")
g_block = "博彩频道"
ch_block = "某购物推广"
g_ok, ch_ok = "央视IPV4", "CCTV1"
print("  分组'博彩频道'会被过滤:", any(k in g_block for k in flt.live_block_group))
print("  频道'某购物推广'会被过滤:", any(k in ch_block for k in flt.live_block_ch))
print("  CCTV不在屏蔽列表:", not (any(k in ch_ok for k in flt.live_block_ch)))

print()
print("全部通过" if bad == 0 and allok else "存在问题，见上")