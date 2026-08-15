#!/bin/sh
# VPS 上拉取最新代码并重启服务。首次部署见 docs/DEPLOY.md。
#
# 在本机构建而非拉 GHCR 镜像：fundmesh 是私有仓库，私有包要占 GHCR 那 500MB
# 配额（polyorders 已在用），而带 pandas/akshare 的镜像压缩后约 300MB，存不下几个版本。
set -e
cd "$(dirname "$0")/.."

git pull --ff-only

# 先构建，构建失败则线上仍跑旧容器，等于什么都没发生
docker compose -f docker-compose.vps.yml build web

# 表结构由应用启动时的 init_db() 建立，无需单独的迁移步骤
docker compose -f docker-compose.vps.yml up -d
docker image prune -f

echo "deployed: $(git rev-parse --short HEAD)"
docker compose -f docker-compose.vps.yml ps
