# PhotoSweep

PhotoSweep 是一个轻量、现代、安全的 NAS Web 照片清理工具。V1 专注于：

- 手机截图与屏幕录制
- SHA-256 验证的完全重复照片和视频
- 微信、QQ 以及少量已知社交 App 保存的图片
- 可恢复的 PhotoSweep 回收站

它不会调用 Synology Photos 私有 API，不会使用 AI、相似照片、人脸识别、OCR，
也不会自动删除文件。

## 群晖直接安装

公共多架构镜像：

```text
ghcr.io/luochen3301/photosweep:latest
```

- 旧版群晖「Docker」图形界面：在「映像」中从上述 URL 添加，按文档映射四个目录后启动。
- 新版 Container Manager：使用 [`docker-compose.synology.yml`](docker-compose.synology.yml) 和 [`synology.env.example`](synology.env.example)。
- 完整步骤见 [`docs/SYNOLOGY.md`](docs/SYNOLOGY.md)。

镜像同时支持 `linux/amd64` 和 `linux/arm64`，由 GitHub Actions 自动构建。首次建议把相册目录只读挂载；PhotoSweep 不需要 Docker Socket，也不要使用高权限模式。
## 快速启动

### 1. 准备目录

```powershell
Copy-Item .env.example .env
```

在 `.env` 中设置宿主机路径：

```dotenv
PHOTOS_PATH=D:/Photos
PHOTOSWEEP_DATA_PATH=D:/PhotoSweep/data
PHOTOSWEEP_CACHE_PATH=D:/PhotoSweep/cache
PHOTOSWEEP_TRASH_PATH=D:/PhotoSweep/trash
PUID=1000
PGID=1000
PHOTOSWEEP_PORT=8080
```

Library 在 PhotoSweep 中填写的是容器路径，例如 `/media/photos`，不是
`D:/Photos`、`/volume1/photo` 等宿主机路径。

### 2. 启动

```powershell
docker compose up --build -d
docker compose ps
```

浏览器打开 `http://NAS-IP:8080`，首次启动会要求创建单管理员账号。

Windows 首次安装 Docker Desktop 时，如果 WSL2/Virtual Machine Platform 尚未启用，
请先完成系统组件安装并重启 Windows，再执行上述命令。

### 3. 完成首次安装

创建管理员后会自动进入安装向导：

1. PhotoSweep 检测 `/media` 下已经映射的目录。
2. 从安全目录树中选择目录，不需要手工输入容器路径。
3. 查看真实的可读/可写状态并创建 Library。
4. 明确确认后启动首次 Full Scan。

如果 `/media` 为空，向导会显示群晖 Container Manager 映射说明。可以选择“稍后配置”进入
总览，页面会保留“继续安装”入口。PhotoSweep 不接触 Docker Socket，也不能从容器内部修改
群晖的卷映射。

## 数据与权限

| 容器路径 | 用途 | 建议权限 |
|---|---|---|
| `/media/*` | 照片库 | 需要清理时读写；只扫描可只读 |
| `/app/data` | SQLite、会话和迁移状态 | 读写 |
| `/app/cache` | 懒生成缩略图 | 读写 |
| `/trash` | PhotoSweep 回收站 | 读写 |

PhotoSweep 启动时根据 `PUID`、`PGID` 修正 data/cache/trash 权限，随后立即降权运行。
Synology 和 Unraid 部署时请填写对照片目录有读取权限的专用低权限用户。

只读媒体挂载示例：

```yaml
volumes:
  - /volume1/photo:/media/photos:ro
  # Synology Photos 个人空间也可以使用：
  # - /volume1/homes/<DSM用户名>/Photos:/media/personal:ro
```

只读 Library 可以扫描，但界面会禁用移动、恢复和永久删除。

## Synology Container Manager

1. 将项目复制到 NAS，或使用 Git 克隆。
2. 在 Container Manager 的“项目”中导入 `docker-compose.yml`。
3. 将 `PHOTOS_PATH` 指向 `/volume1/photo`、个人 Photos 目录或其他明确目录。
4. 为 data/cache/trash 创建独立目录并授权给 `PUID/PGID`。
5. 建议通过 DSM 反向代理提供 HTTPS，并把
   `PHOTOSWEEP_COOKIE_SECURE=true`。

PhotoSweep 不会自动发现 Synology Photos 路径，也不会绕过 DSM 权限。

## Unraid

可以通过 Compose Manager 导入本项目，或从 Dockerfile 构建。建议映射：

```text
/mnt/user/photos        -> /media/photos
/mnt/user/appdata/photosweep/data  -> /app/data
/mnt/user/appdata/photosweep/cache -> /app/cache
/mnt/user/photosweep-trash         -> /trash
```

将网络类型保持为 bridge，只暴露端口 `8080`，再由反向代理提供 HTTPS。

## 本地开发

后端：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:PHOTOSWEEP_ALLOWED_MEDIA_ROOTS="K:\path\to\test-media"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\uvicorn.exe app.main:app --app-dir backend --reload
```

前端：

```powershell
cd frontend
npm install
npm run dev
```

Vite 会把 `/api` 和 `/health` 代理到 `127.0.0.1:8000`。

## 安全设计

- 所有修改请求都需要登录会话和 CSRF token。
- Library 与媒体路径必须经过真实路径边界检查。
- 扫描器不跟随目录符号链接。
- Move 前重新检查 size 和纳秒 mtime。
- 跨文件系统先复制、fsync、验证大小和 SHA-256，再删除 Source。
- Restore 不覆盖同名文件，会生成 `_restored_1`。
- Permanent Delete 只允许处理数据库登记的 Trash 项目，并要求密码复核。
- 容器使用只读根文件系统、非 root 用户、移除 Linux capabilities。

## 验证

```powershell
.\.venv\Scripts\ruff.exe check backend
.\.venv\Scripts\pytest.exe -q
$env:PYTHONPATH="backend"
.\.venv\Scripts\python.exe scripts\benchmark_100k.py
cd frontend
npm run typecheck
npm run build
npm run test:sites
```

## V1 明确不包含

CLIP、SigLIP、大语言模型、人脸识别、相似照片、连拍最佳选择、模糊/黑屏/
曝光分析、OCR、二维码、RAW 分析、摄影质量评分、自动删除和定时过期。
