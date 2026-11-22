# -*- coding: utf-8 -*-
"""验证 absolutize 相对路径重写与产物结构"""
import json, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LD = os.path.join(ROOT, "output", "lines")

print("== 1. 线路文件相对路径重写检查 ==")
total_rel_spider, total_rel_ext = 0, 0
files = sorted(f for f in os.listdir(LD) if f.endswith(".json"))
for fn in files:
    d = json.load(open(os.path.join(LD, fn), encoding="utf-8"))
    sp = str(d.get("spider", "") or "")
    rel_sp = bool(sp) and not sp.startswith(("http", "{", "[")) and ";" not in sp[:6]
    # spider 纯 md5（无路径分隔）视为原样保留（如 7dddc1aedbbe...）
    rel_ext = 0
    for s in d.get("sites", []) or []:
        ext = s.get("ext")
        if isinstance(ext, str) and ext and ext.startswith("./"):
            rel_ext += 1
        if isinstance(ext, dict):
            j = str(ext.get("jar", ""))
            if j.startswith("./"):
                rel_ext += 1
    if rel_sp or rel_ext:
        total_rel_spider += 1 if rel_sp else 0
        total_rel_ext += rel_ext
        print(f"  {fn}: 相对spider={rel_sp}({sp[:40]}) 相对ext={rel_ext}")
print(f"  汇总：仍有相对spider的线路 {total_rel_spider} 个，相对ext站点 {total_rel_ext} 个")

print()
print("== 2. 老刘备线路 spider 重写结果（原为 ./fty.jar;md5;...） ==")
for fn in files:
    d = json.load(open(os.path.join(LD, fn), encoding="utf-8"))
    if "fty.jar" in str(d.get("spider", "")):
        print("  ", fn, "spider =", d["spider"][:80])

print()
print("== 3. merged.json 结构 ==")
m = json.load(open(os.path.join(ROOT, "output", "merged.json"), encoding="utf-8"))
sp = str(m.get("spider", "") or "")
print("  sites:", len(m["sites"]), "| spider:", (sp[:70] or "(无)"),
      "| spider是绝对URL:", sp.startswith("http"))
rel = sum(1 for s in m["sites"] if isinstance(s.get("ext"), str) and s["ext"].startswith("./"))
print("  相对ext站点:", rel)

print()
print("== 4. private.json（分拣产物） ==")
p = os.path.join(ROOT, "output", "private.json")
if os.path.exists(p):
    d = json.load(open(p, encoding="utf-8"))
    print("  站点数:", len(d.get("sites", [])))
    names = [s["name"] for s in d.get("sites", [])][:12]
    print("  站点名样例:", names)
    # 泄漏检查：公开产物里不应含这些站点 key
    pub_keys = {s.get("key") for s in m["sites"]}
    leak = [s["name"] for s in d.get("sites", []) if s.get("key") in pub_keys and s["key"].startswith("auto")]
    print("  与公开单仓key重叠（同key不同站点）:", len(leak))
else:
    print("  未生成（本次无命中分拣词的站点）")

print()
print("== 5. duocang 与 auto_sources ==")
dc = json.load(open(os.path.join(ROOT, "output", "duocang.json"), encoding="utf-8"))
print("  多仓线路:", len(dc["urls"]), "| 绝对URL:", sum(1 for u in dc["urls"] if str(u["url"]).startswith("http")))
au = json.load(open(os.path.join(ROOT, "config", "auto_sources.json"), encoding="utf-8"))
print("  auto_sources 纳入:", [s["name"] for s in au["sources"]])

print()
print("== 6. 网页/README 零提及检查 ==")
for f, kw in [("index.html", "private.json"), ("README.md", "private.json")]:
    t = open(os.path.join(ROOT, f), encoding="utf-8").read()
    print(f"  {f} 含 private.json 字样: {kw in t}")
# status.json 不应暴露分拣统计明文（字段名中性即可）
st = json.load(open(os.path.join(ROOT, "output", "status.json"), encoding="utf-8"))
print("  status.json stats 字段:", list(st.get("stats", {}).keys()))
