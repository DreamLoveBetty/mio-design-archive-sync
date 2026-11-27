# Mio Design Archive Sync

将 [霧島澪 · 姿势与服装设计档案 (`sleepyzone.online/mio-design-archive/`)](https://sleepyzone.online/mio-design-archive/) 的全部结构化数据（系列 / 分卷 / 九宫格总览图 / 单件图 / 中英 Prompt 与负向词）镜像到本地，并增量同步至 Google Drive。

- 仅依赖 Python 3.9+ 标准库，无第三方包、无硬编码账号/Token/网盘目录 ID。
- 其它用户获取本项目后，只需使用自己的 Google Drive 授权即可将源站资料按相同目录结构同步到自己的网盘。

## 同步后的目录结构

```text
Mio-Design-Archive/
├── README.md                           # 全库总览、统计与各卷导航索引
├── manifest.json                       # 源站 data.js + tagdict.js 完整数据快照
├── 01_发型_hair/
│   └── Vol.01_长发-刘海与层次/
│       ├── 00_sheet_hair-01.jpg        # 九宫格总览图
│       ├── README.md                   # 本卷说明、原帖链接、每件的中英 Prompt 与负向词
│       ├── 01_H-01-01_Light_Bangs_Straight.jpg
│       └── ...
├── 02_服装_cloth/
│   └── ...
└── 03_姿势_pose/
    └── ...
```

## 快速使用

### 1. 在 Antigravity 环境同步（使用 `gdrive` CLI）

```bash
python3 sync.py
```

默认将 `Mio-Design-Archive/` 同步到当前登录 Google Drive 的根目录。如果存在多个已绑定账号，可用 `AGY_ACCOUNT=your@gmail.com python3 sync.py`。

### 2. GitHub Actions 自动定时同步（推荐 · 关机也可云端运行）

项目内置 [`.github/workflows/sync.yml`](.github/workflows/sync.yml)，会在云端临时 Runner 中运行，**不会向 GitHub 仓库提交任何图片或网盘数据**，所有资料只存放在你的 Google Drive：

- **定时频率**：每天自动运行一次（UTC 20:00 / 北京时间 04:00），也支持在 GitHub **Actions** 页面手动点击 **Run workflow** 触发（支持 `dry_run` 与 `force_update` 参数）。
- **轻量增量检查**：默认先通过 `rclone cat` 读取网盘内现有的 `manifest.json` 与源站比对；**源站无更新时几秒内直接退出，不下载也不上传图片**；有更新时仅拉取新增或变更分卷并增量同步。

#### 配置步骤（只需做一次）

1. 在本机安装 `rclone` 后获取 Google Drive 授权配置（或在终端执行 `rclone config` 创建名为 `gdrive` 的 Google Drive remote，或直接执行）：
   ```bash
   rclone authorize "drive"
   ```
   浏览器完成授权后，将终端输出的完整 JSON token 按如下格式整理为一段 `rclone.conf` 内容：
   ```ini
   [gdrive]
   type = drive
   scope = drive
   token = {"access_token":"...","token_type":"Bearer","refresh_token":"...","expiry":"..."}
   ```
2. 在你的 GitHub 仓库中打开 **Settings → Secrets and variables → Actions**：
   - 在 **Secrets** 下新建 **`RCLONE_CONFIG`**，粘贴上面整段配置内容。
   - （可选）如果目标网盘路径不是 `gdrive:Mio-Design-Archive`，可在 **Variables** 下新建 **`RCLONE_REMOTE`** 覆盖默认路径。
3. 打开仓库的 **Actions** 页，选择 **Sync Mio Design Archive to Google Drive**，点击 **Run workflow** 验证一次即可。

> **提示**：GitHub 对连续 **60 天没有任何新 commit** 的仓库会自动暂停 `schedule` 定时触发；若收到 GitHub 通知或发现暂停，进入 Actions 页面点击 **Enable workflow** 即可恢复。

### 3. 在普通本机 / 服务器同步（使用 `rclone`）

先通过 `rclone config` 配置好自己的 Google Drive remote（例如命名为 `gdrive`），再执行：

```bash
python3 sync.py --backend rclone --rclone-remote gdrive:Mio-Design-Archive
```

### 4. 仅下载到本地镜像

```bash
python3 sync.py --backend local --mirror-dir ./Mio-Design-Archive
```

### 5. 从已有本地备份目录直接上传（源站离线时可用）

```bash
python3 sync.py --from-local ./mirror/Mio-Design-Archive --backend gdrive
```

## 常用参数

- `--parent-id <FOLDER_ID>`：同步到 Google Drive 指定父文件夹下（不传则在根目录创建 `Mio-Design-Archive`）。
- `--root-name <NAME>`：自定义目标根目录名称（默认 `Mio-Design-Archive`）。
- `--series {all,hair,cloth,pose}`：仅同步指定系列，或全部同步（默认 `all`）。
- `--with-txt`：在每张单件图旁额外生成同名 `.txt`（仅含英文 Prompt，适合训练打标）。
- `--dry-run`：仅拉取本地镜像并打印计划动作，不修改 Google Drive。
- `--force-update`：忽略本地缓存与远程跳过规则，强制重新下载并覆盖上传。
- `--no-remote-check`：在使用 `rclone` 后端时跳过远程 `manifest.json` 预检，直接构建完整镜像再比对上传。
- `--download-workers N` / `--upload-workers N`：并发下载/上传线程数（默认 `10` / `5`）。

## 增量同步与容灾说明

- **图片增量跳过**：本地会校验 JPEG 文件头避免重复下载；Google Drive 端若同名图片已存在则跳过上传，避免产生重复文件。
- **文档原位更新**：`README.md` 与 `manifest.json` 通过 `.sync_state.json` 记录 SHA-256 摘要，仅在内容变化时调用原位更新（保留 Drive 文件 ID 与链接）。
- **源站依赖与离线备份**：直接同步依赖源站 `https://sleepyzone.online/mio-design-archive/` 在线且 `data.js` 结构兼容；建议将 `mirror/Mio-Design-Archive` 保留或导出后供 `--from-local` 离线复用。
