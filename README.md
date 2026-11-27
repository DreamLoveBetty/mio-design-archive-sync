# Mio Design Archive Sync

直接将 [霧島澪 · 姿势与服装设计档案 (`sleepyzone.online/mio-design-archive/`)](https://sleepyzone.online/mio-design-archive/) 的结构化数据与图片增量同步到本仓库 `archive/` 目录，作为公开数据源供 CanvasHub 等客户端零配置增量拉取。

- 仅依赖 Python 3.9+ 标准库，无第三方依赖、无网盘或账号 Secret 需求。
- 原站无更新且图片齐全时几秒内直接退出；新增分卷时仅下载新增图片并用 `GITHUB_TOKEN` 自动提交。

## 仓库内容结构

```text
archive/
├── README.md                 # 全库索引与分卷列表
├── manifest.json             # 源站完整数据快照（volumes / items / tagdict / prefix / negative）
└── assets/
    ├── sheets/               # 各卷九宫格总览图 (<slug>.jpg)
    └── tiles/                # 各单件高清参考图 (<slug>-<no>.jpg)
```

- 公开 Raw 清单地址：`https://raw.githubusercontent.com/DreamLoveBetty/mio-design-archive-sync/main/archive/manifest.json`
- 公开图片 Raw 前缀：`https://raw.githubusercontent.com/DreamLoveBetty/mio-design-archive-sync/main/archive/`

## 自动同步（GitHub Actions）

[`.github/workflows/sync.yml`](.github/workflows/sync.yml) 每天 UTC 20:00（北京时间 04:00）自动运行，也支持在 **Actions** 页面手动点击 **Run workflow** 触发（支持 `force_update` 参数）。

## 本地手动运行

```bash
python3 sync.py
```

可选参数：

- `--archive-dir <path>`：指定输出目录（默认 `./archive`）
- `--download-workers <N>`：并发下载线程数（默认 `10`）
- `--force-update`：忽略已有有效图片并强制重新下载
- `--dry-run`：仅检查并打印待下载数量，不写磁盘
