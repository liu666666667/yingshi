# yingshi · 影视线路聚合

> 每日自动采集全网可用影视仓/TVBox 线路与直播源，生成三类可直接导入的订阅产物。
> 屏蔽规则全外置，广告 / 博彩 / 垃圾内容按规则过滤。仅供学习交流，请勿商用。

## 三类产物

| 产物 | 说明 | 地址 |
| --- | --- | --- |
| 多仓订阅 | 收录全部可用线路 | `output/duocang.json` |
| 合并单仓 | 可合并站点合成一个单仓，超过 1500 站点自动分卷 | `output/merged.json`（满卷后另有 `merged2.json`…） |
| 直播源 m3u | 每日抓取去重、按频道分组 | `output/live.m3u`（另有 `live.txt`） |

导航页：`index.html`（GitHub Pages 开启后即为主页），每条线路都提供 **直连 / 加速** 两种链接，一键复制。
国内访问 GitHub 不稳定时，在导航页切换「加速模式」，全部链接自动变为镜像地址。

## 使用方法（影视仓示例）

1. 打开导航页（Pages 链接）；
2. 选择「直连模式」或「加速模式」；
3. 点击对应产物右侧「复制链接」；
4. 影视仓 → 设置 → 配置地址 → 粘贴。

## 自动更新

- GitHub Actions 每日北京时间 04:10 自动运行：**全网自动搜索发现新源 → 采集 → 校验 → 过滤 → 相对路径重写 → 去重 → 生成产物 → 提交 → 重新部署 Pages**；
- 支持手动触发（Actions → daily-update → Run workflow）。

## 每日全网源发现（全自动，无需人工维护清单）

- 每天由 `scripts/discover.py` 自动在四个通道全网上搜新源：**内置种子**（含用户自己的 `yingshicang` 仓库，仅冷启动）→ **GitHub 仓库搜索**（最近更新的相关仓库 README）→ **Bing + DuckDuckGo 网页搜索** → **Gitee 仓库搜索**（国内兑底）；
- 候选地址自动探活（App UA 轮换防伪装反爬 + TVBox 格式校验 + 站点数门槛），新源写入 `config/auto_sources.json`（点播）/ `config/auto_lives.json`（直播）**自生长清单**；
- 每次运行对存量源做健康检查，**连续 7 次失效自动淘汰**；总量上限（点播 80 / 直播 30）超限自动裁剪——新增、去重、淘汰全部自动完成，无需维护任何清单；
- 新源当日即参与采集并进入多仓；各通道统计见 `output/discovery_report.json`。

## 源分三组，分开维护

| 组 | 维护方式 | 独立产物 | 进公开页面 |
| --- | --- | --- | --- |
| manual | `config/sources.json` 手动维护 | `output/manual.json` | 是 |
| own | `config/own_sources.json` 自己维护 | `output/own.json` | 否 |
| auto | discover.py 每日全网搜索自动维护 | （并入主多仓） | 是 |

三组的站点都会进入**最终线路**（合并单仓 merged*.json / 全量版 merged_all.json）做统一去重合并——直接用最终线路即可，分组只为分开维护。

## 配置文件（全部外置，无硬编码）

| 文件 | 作用 |
| --- | --- |
| `config/sources.json` | 手动维护源清单（产出 output/manual.json） |
| `config/own_sources.json` | 自己维护的线路源（产出 output/own.json，页面零展示；站点仍参与最终线路去重合并） |
| `config/auto_sources.json` | 每日全网搜索自动发现的新源（自生长清单，全自动维护） |
| `config/auto_lives.json` | 每日全网搜索自动发现的直播源（自生长清单，全自动维护） |
| `config/discovery.json` | 自动搜索参数（单次纳入上限等，可选调节） |
| `config/live_sources.json` | 直播源清单 |
| `config/rules.json` | 公开过滤规则：屏蔽词 / 域名 / 正则 / 白名单 |
| `config/sensitive.json.example` | 本地扩展配置模板（见下） |

### 内容治理

- `rules.json` 中 `block_keywords / block_domains / block_regex` 命中的站点直接丢弃，`whitelist_*` 优先级最高；
- 直播源按分组名与频道名过滤，URL 全局去重，同名频道最多保留 3 条线；
- 特定内容分类站点会被自动分拣到独立产物文件，不进入多仓 / 合并单仓 / 导航页 / 本 README（不在任何公开入口列出）；
- 失效源自动跳过并记录在 `output/status.json`，导航页实时展示采集状态。

### 相对路径重写（导入不报错的关键）

源配置里的 `spider: './xxx.jar'`、站点 `ext: './libs/xx.json'` 等相对路径，
存入本仓库后会 404 导致整仓解析失败。采集时全部自动重写为以原源为基的绝对地址，
保证多仓 / 合并单仓导入影视仓后 jar 与 ext 正常加载。

### 本地扩展配置（可选，不入仓库）

若想在本地额外维护一组只给自己用的源或关键词：

1. 复制 `config/sensitive.json.example` 为 `config/sensitive.json`；
2. 本地运行：`python scripts/update.py --with-sensitive`；
3. 该文件及相关本地目录均已在 `.gitignore` 中，**永远不会被推送**；GitHub Actions 环境没有该文件，线上公开产物不含任何本地扩展内容。

## 本地运行

```bash
python scripts/discover.py                           # 每日全网自动搜索新源（自生长清单）
python scripts/update.py --repo 你的用户名/yingshi   # 生成最终形态链接
python scripts/update.py --base https://xxx          # 指定产物 URL 前缀（测试隧道）
python scripts/update.py --with-sensitive            # 追加本地扩展采集
python scripts/update.py --skip-live                 # 跳过直播（调试）
python scripts/update.py --skip-discovery            # 跳过纳入自动发现源（调试）
```

无任何第三方依赖，Python 3.9+ 标准库即可运行。

## 仓库结构

```
yingshi/
├── config/            # 全部外置配置（sources / auto_sources / auto_lives / discovery / rules）
├── scripts/discover.py # 每日全网自动搜索新源（自生长清单，零人工维护）
├── scripts/update.py  # 采集脚本（本地 / Actions 通用）
├── output/            # 产物：lines/ 分线路 + duocang + merged（多卷）+ live + status
├── index.html         # 导航页（Pages 主页）
└── .github/workflows/update.yml
```

## 声明

本项目仅聚合公开接口状态并做格式整理，内容归原接口作者所有，请于下载后 24 小时内删除且勿用于商业用途。

> AI生成
