# 在群晖 Docker / Container Manager 安装 PhotoSweep

公共镜像：`ghcr.io/luochen3301/photosweep:latest`

支持的架构：`linux/amd64`、`linux/arm64`。

## 方法一：旧版群晖 Docker 图形界面

截图中显示的是旧版「Docker」套件，可按下面步骤安装。

### 1. 下载镜像

进入「映像」→「新增」→「从 URL 添加」，填写：

```text
ghcr.io/luochen3301/photosweep:latest
```

如果旧版 DSM 不接受 GHCR URL，可临时开启 SSH，只执行一次：

```bash
sudo docker pull ghcr.io/luochen3301/photosweep:latest
```

完成后镜像会自动出现在「映像」页面。

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

先确认局域网 HTTP 登录正常，再配置 DSM 反向代理。启用 HTTPS 后把 `PHOTOSWEEP_COOKIE_SECURE` 改为 `true` 并重建容器。HTTP 环境提前设置为 `true` 会导致浏览器不发送登录 Cookie。
