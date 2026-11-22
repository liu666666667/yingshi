# -*- coding: utf-8 -*-
"""分拣（separate → output/private.json）机制单测：内存级验证，不动产物"""
import importlib.util, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("upd", os.path.join(ROOT, "scripts", "update.py"))
upd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upd)

flt = upd.Filter(os.path.join(ROOT, "config", "rules.json"))

fake = {
    "spider": "./demo.jar;md5;abc",
    "sites": [
        {"key": "k1", "name": "普通影院", "api": "csp_XBPQ", "ext": "./libs/1.json"},
        {"key": "k2", "name": "普通搜索", "api": "csp_XX"},
        {"key": "k3", "name": "麻豆星空", "api": "csp_X"},
        {"key": "k4", "name": "天美传媒", "api": "csp_X"},
        {"key": "k5", "name": "腾讯视频", "api": "csp_X"},
        {"key": "k6", "name": "百家乐娱乐城", "api": "csp_X"},
    ],
    "lives": [{"name": "直播", "type": 0}],
}
base = "https://example.com/cfg.json"
data = upd.absolutize(fake, base)
pub, sep, iso, blocked = upd.clean_single_config(data, flt)

names_pub = [s["name"] for s in pub["sites"]]
names_sep = [s["name"] for s in sep["sites"]]
print("公开站点:", names_pub)
print("分拣站点:", names_sep)
print("屏蔽数:", blocked)
assert "百家乐娱乐城" not in names_pub + names_sep, "博彩应被屏蔽"
assert names_sep == ["麻豆星空", "天美传媒"], "分拣应只含命中词站点: got %s" % names_sep
assert "麻豆星空" not in names_pub, "分拣站点不应出现在公开"

merged = upd.merge_single_configs([("l001_x.json", "测试源", pub)])
sep_merged = upd.merge_single_configs([("l001_x.json", "测试源", sep)])
assert isinstance(merged, list) and isinstance(sep_merged, list), "merge 现在返回多卷列表"
all_pub_sites = [s for m in merged for s in m["sites"]]
all_sep_sites = [s for m in sep_merged for s in m["sites"]]
assert all_pub_sites and not any("麻豆" in s["name"] for s in all_pub_sites), "公开仓不应含分拣站"
assert all_sep_sites and all("麻豆" in s["name"] or "天美" in s["name"] for s in all_sep_sites), "分拣仓应只含命中站"

# 分桶结构单测：own/manual/auto 三组产物隔离，own 参与最终合并去重
col = upd.VodCollector({}, flt, {"sources": [], "live_sources": []})
col.absorb("own_x", "我的自维护源", upd.json.loads('{"sites": [{"key": "ok1", "name": "自有站", "api": "csp_X"}], "lives": []}'), "https://example.com/o.json", group="own")
col.absorb("auto_y", "网络源", upd.json.loads('{"sites": [{"key": "ok2", "name": "网络站", "api": "csp_X"}], "lives": []}'), "https://example.com/a.json", group="auto")
assert len(col.public["own"]) == 1 and len(col.public["auto"]) == 1 and not col.public["manual"], "分桶失败"
final = upd.merge_single_configs(col.public["auto"] + col.public["own"])
final_names = [s["name"] for m in final for s in m["sites"]]
assert "自有站" in final_names and "网络站" in final_names, "own 未参与最终合并去重"
print("分桶验证: own/auto 分组正确，own 参与最终合并去重 ✓")
print("spider 重写:", data["spider"])
assert data["spider"] == "https://example.com/demo.jar;md5;abc"
assert data["sites"][0]["ext"] == "https://example.com/libs/1.json"
print()
print("分拣机制 + absolutize 单测全部通过")
