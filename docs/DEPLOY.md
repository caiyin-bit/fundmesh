# FundMesh 部署手册

目标机：`141.164.53.169`（首尔 VPS，与 polyorders 同机）
访问方式：Cloudflare Tunnel → 域名

## 架构

```
Cloudflare（DNS + TLS + 边缘）
      │  隧道为出站长连接，VPS 不开放任何入站端口
      ▼
 cloudflared ──► web (FastAPI :8000) ──► db (TimescaleDB + 数据卷)
```

两个不同于 polyorders 的决定，原因都是这台机器的现状：

- **用 Cloudflare Tunnel 而非 Caddy**：polyorders 的 Caddy 已占用 80/443，再起一个反代必然冲突。隧道是出站连接，不占端口、不需要源站证书，也不必改动 polyorders 的任何配置。
- **在 VPS 上构建而非拉 GHCR 镜像**：fundmesh 是私有仓库，私有包占用 GHCR 那 500MB 配额（polyorders 已在用），而带 pandas/akshare 的镜像约 453MB，存不下几个版本。

---

## 一、Cloudflare 侧（控制台操作）

1. 进入 **Zero Trust → Networks → Tunnels → Create a tunnel**，类型选 **Cloudflared**
2. 命名如 `fundmesh`，创建后选择 **Docker** 安装方式，复制命令里 `--token` 后面那一长串
3. 在 **Public Hostnames** 添加一条：
   - Subdomain: `fundmesh`，Domain: `bluesignals.cc`（与 polyorders 同域）
   - Type: `HTTP`，URL: `web:8000`
     （`web` 是 compose 里的服务名，cloudflared 与它在同一网络，按服务名解析）
4. DNS 记录由隧道自动创建，无需手工添加

> 域名可换成你想要的任意子域，只要该域已托管在 Cloudflare。

## 二、VPS 首次部署

```sh
ssh root@141.164.53.169

# 1. 取代码。仓库是私有的，需要一把只读部署密钥：
#    ssh-keygen -t ed25519 -f ~/.ssh/fundmesh_deploy -N ''
#    把 ~/.ssh/fundmesh_deploy.pub 加到 GitHub 仓库 Settings → Deploy keys（只读即可）
mkdir -p /opt/fundmesh && cd /opt/fundmesh
git clone git@github.com:caiyin-bit/fundmesh.git app
cd app

# 2. 填配置
cp .env.vps.example .env
vi .env          # POSTGRES_PASSWORD 自己设一个强密码；CF_TUNNEL_TOKEN 填第一步复制的

# 3. 启动
docker compose -f docker-compose.vps.yml up -d --build

# 4. 检查
docker compose -f docker-compose.vps.yml ps          # 三个服务都应 Up，db 为 healthy
docker compose -f docker-compose.vps.yml logs -f web # 首启会建表并同步交易日历
```

表结构由应用启动时的 `init_db()` 建立，没有单独的迁移步骤。它会检测到 TimescaleDB
扩展并自动把 `nav_history` 转成 hypertable、挂上压缩策略——本地因磁盘问题没能启用的
部分，在这里会直接生效。

浏览器打开 `https://fundmesh.bluesignals.cc` 即可。

## 三、日常发布

```sh
ssh root@141.164.53.169 'cd /opt/fundmesh/app && sh scripts/vps-deploy.sh'
```

拉代码 → 构建 → 重启。构建失败时线上仍跑旧容器，等于什么都没发生。

## 四、备份

`pgdata` 卷是唯一有状态的东西——你的账目和行情历史。建议加一条 cron：

```sh
0 3 * * * cd /opt/fundmesh/app && docker compose -f docker-compose.vps.yml exec -T db \
  pg_dump -U fundmesh fundmesh | gzip > /opt/fundmesh/backup/$(date +\%F).sql.gz
```

留意保留策略与异地副本——只在同一台机器上留备份，机器没了备份也没了。

## 五、已知限制

- **没有任何认证**。当前所有接口对外开放，任何知道域名的人都能查看和修改账本。
  上线后请尽快补上——最省事的过渡方案是在 Cloudflare Zero Trust 里给这个主机名
  加一条 Access 策略（邮箱验证码即可），不需要改代码。
- 数据源全部在国内，从首尔访问的延迟与可用性可用 `scripts/check-datasources.sh` 实测。
- 无自动化测试，发布没有门禁。
