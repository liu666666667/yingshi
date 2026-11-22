# -*- coding: utf-8 -*-
"""验证敏感隔离机制：公开产物零包含，私有产物正常生成"""
import json, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("== 私有产物是否存在 ==")
pv = os.path.join(ROOT, "output", "private")
for root, _, files in os.walk(pv):
    for f in files:
        print("  ", os.path.relpath(os.path.join(root, f), pv), os.path.getsize(os.path.join(root, f)), "bytes")

print("== 私有多仓内容 ==")
pd = os.path.join(pv, "duocang_private.json")
if os.path.exists(pd):
    d = json.load(open(pd, encoding="utf-8"))
    for u in d["urls"]:
        print("  ", u["name"], "->", u["url"])

print("== 私有合并单仓：命中分流词的站点 ==")
pm = os.path.join(pv, "merged_private.json")
if os.path.exists(pm):
    m = json.load(open(pm, encoding="utf-8"))
    hits = [s["name"] for s in m["sites"] if "豆瓣" in s["name"]]
    print("   sites总数:", len(m["sites"]), "| 含'豆瓣'的站点:", len(hits), hits[:5])
    priv_src = [s["name"] for s in m["sites"]][:3]
    print("   私有源站点样例:", priv_src)

print("== 公开产物泄漏检查（关键！） ==")
leak = 0
for root, _, files in os.walk(os.path.join(ROOT, "output")):
    if "private" in root:
        continue
    for f in files:
        if not f.endswith((".json", ".m3u", ".txt")):
            continue
        p = os.path.join(root, f)
        try:
            t = open(p, encoding="utf-8").read()
        except Exception:
            continue
        if "本地私测源" in t or "testpriv" in t:
            leak += 1
            print("  LEAK:", os.path.relpath(p, ROOT))
m = os.path.join(ROOT, "output", "merged.json")
if os.path.exists(m):
    t = json.load(open(m, encoding="utf-8"))
    still = [s["name"] for s in t["sites"] if "豆瓣" in s["name"]]
    print("   公开合并单仓里'豆瓣'站点残留:", len(still), still[:3])
print("   私有源泄漏条目:", leak)

print("== gitignore 保护检查 ==")
gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
need = ["config/sensitive.json", "output/private/", "raw/"]
for n in need:
    print("   .gitignore 含", n, ":", n in gi)
