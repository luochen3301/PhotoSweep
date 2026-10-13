# 在群晖 Docker / Container Manager 安装 PhotoSweep

公共镜像：`docker.io/luochen23151/photosweep:latest`

支持的架构：`linux/amd64`、`linux/arm64`。

## 方法一：旧版群晖 Docker 图形界面

截图中显示的是旧版「Docker」套件，可按下面步骤安装。

### 1. 下载镜像

旧版「映像 → 新增 → 从 URL 添加」使用 Docker Hub 页面地址，而不是 GHCR 镜像名称。可粘贴：

```text
https://hub.docker.com/r/luochen23151/photosweep
```

点击「新增」后选择 `latest` 标签并等待下载完成。不要勾选「信任的 SSL 自签名证书」。

这个 URL 不能保证每台旧版 DSM 都能成功查询注册表。如果出现“查询注册表失败”，
不要反复更换同一个仓库 URL，也不要关闭证书校验或开启容器高权限。
错误发生在下载镜像前，尚不是 PhotoSweep 容器运行错误；仅凭这条消息不能断定是
NAS 网络、DNS、Docker Hub 限制还是旧套件兼容问题。

### 无需连接 Docker Hub：导入已验证镜像文件

项目的 `Verify PhotoSweep` 工作流会在容器验证成功后生成离线镜像文件：

1. 在 GitHub 项目的 Actions 中打开已通过的验证记录，下载 `photosweep-amd64-image`
   （Intel/AMD 群晖）或 `photosweep-arm64-image`（确认支持该架构和 Docker 的 ARM64 设备）。
2. 解压下载的 ZIP，得到 `.tar` 和 `.tar.sha256`；不要把 ZIP 当成镜像文件。
3. 在群晖「映像 → 新增/导入 → 从文件添加」中选择 `.tar`，不是「容器 → 导入」。
4. 导入后镜像名为 `photosweep:verified-amd64` 或 `photosweep:verified-arm64`，
   按下面相同的端口和卷映射启动即可。

不确定架构时，SSH 执行 `uname -m`：`x86_64` 对应 amd64，`aarch64` 对应 arm64；
不要使用 arm64 镜像尝试运行在 32 位 ARM 设备上。Actions 文件有保留期限，请保存本地副本。

这类归档由 `docker image save` 生成，不能替换成 `docker export` 导出的容器文件系统。
参考：[Docker 镜像归档说明](https://docs.docker.com/reference/cli/docker/image/save/)、
[Synology 镜像导入说明](https://kb.synology.com/en-us/DSM/help/ContainerManager/docker_image?version=7)。

### 2. 启动容器

选中镜像并点击「启动」，容器名称填写 `photosweep`，开启自动重新启动，不要启用高权限。

端口映射：

| 本地端口 | 容器端口 | 协议 |
|---|---:|---|
| `8080` | `8080` | TCP |

卷映射：

| 群晖目录 | 容器目录 | 权限 |
|---|---|---|
| `/volume1/homes/<用户名>/Photos` | `/media/photos` | 只读 |
| `/volume1/docker/photosweep/data` | `/app/data` | 读写 |
| `/volume1/docker/photosweep/cache` | `/app/cache` | 读写 |
| `/volume1/docker/photosweep/trash` | `/trash` | 读写 |

共享空间相册可把第一行换成 `/volume1/photo`。首次使用建议保持只读；确认扫描结果后，才把相册卷改为读写。

环境变量：

| 名称 | 值 |
|---|---|
| `PUID` | DSM 用户的数字 UID |
| `PGID` | DSM 用户的数字 GID |
| `TZ` | `Pacific/Auckland` 或你的时区 |
| `PHOTOSWEEP_ALLOWED_MEDIA_ROOTS` | `/media` |
| `PHOTOSWEEP_DATA_DIR` | `/app/data` |
| `PHOTOSWEEP_CACHE_DIR` | `/app/cache` |
| `PHOTOSWEEP_TRASH_DIR` | `/trash` |
| `PHOTOSWEEP_COOKIE_SECURE` | 初次局域网 HTTP 使用 `false` |

可通过 SSH 查询 UID/GID：

```bash
id <DSM用户名>
```

不要使用「高权限执行容器」。PhotoSweep 只在启动时使用必要权限修正 data/cache/trash 目录所有权，随后立即降权到 PUID/PGID 运行；它不会修改相册目录权限。

### 3. 完成首次安装

访问 `http://群晖IP:8080`，创建管理员，然后在安装向导中选择 `/media/photos` 并明确启动 Full Scan。

## 方法二：Container Manager 项目 / Compose

1. 创建 `/volume1/docker/photosweep`、`data`、`cache`、`trash`。
2. 下载 `docker-compose.synology.yml` 和 `synology.env.example`。
3. 把 `synology.env.example` 改名为 `.env`，填写真实用户名路径和 PUID/PGID。
4. 在 Container Manager → 项目中导入 Compose，或通过 SSH 执行：

```bash
docker compose --env-file .env -f docker-compose.synology.yml pull
docker compose --env-file .env -f docker-compose.synology.yml up -d
```

## 更新

Compose：

```bash
docker compose --env-file .env -f docker-compose.synology.yml pull
docker compose --env-file .env -f docker-compose.synology.yml up -d
```

旧版 Docker 图形界面：下载最新 `latest` 镜像，停止并重新创建容器；data/cache/trash 使用宿主机卷，因此管理员账号、数据库和缓存不会随容器重建而丢失。

## HTTPS

先确认局域网 HTTP 登录正常，再配置 DSM 反向代理。启用 HTTPS 后设置并重建容器：

```dotenv
PHOTOSWEEP_COOKIE_SECURE=true
PHOTOSWEEP_PUBLIC_ORIGIN=https://photos.example.com
```

PUBLIC_ORIGIN 是浏览器的完整访问源，不包含路径；如果用了 `8443` 之类非标准端口，
填写 `https://photos.example.com:8443`。它用于同源检查，避免代理后端使用 HTTP 时误拒绝 HTTPS 登录。
如需信任客户端转发头，可将 `FORWARDED_ALLOW_IPS` 设为实际代理 IP；不要设为 `*`。
仅局域网 HTTP 时保持 COOKIE_SECURE=false 且 PUBLIC_ORIGIN 留空。

HTTP 环境提前设置 COOKIE_SECURE=true 会导致浏览器不发送登录 Cookie。
不要将无 HTTPS 的登录端口直接暴露到互联网。

## 更新前备份与权限变化

先停止 PhotoSweep，备份 data 和 trash 目录，再替换镜像并保留原卷映射。
新版本迁移数据库前还会保存 data/pre-upgrade-*.db，但这不包含照片或 Trash 文件。
初次用只读挂载验证结果后，需要清理时再明确改为读写并重建容器；刷新图库权限后界面才启用移动。
PhotoSweep 回收站不是群晖共享文件夹的 `#recycle`，不要在 DSM 中误清空它。

若设置页显示“未完成的文件操作”，请保留原件和所有副本、查看容器日志并核对路径。
系统不会为消除警告自动删除文件，不应通过清空数据库或 trash 来处理这个提示。
