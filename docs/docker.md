# Docker 部署详解

> 快速版看 [README 的「快速开始」](../README.md#-快速开始docker)，这里是一些补充。

## 目录结构（容器视角）

```
/data                        ← 挂载出来的数据目录，备份它就行
├── config.json              配置（好友 / 消息 / 视频分享 / 定时）
├── secrets/cookie.json      抖音 Cookie（权限 600，别外传）
└── artifacts/               运行日志、result.json、失败截图、trace
```

`/app` 里是代码（镜像内），改了代码要重新 build 镜像。

## 常用运维命令

```bash
docker compose ps                    # 状态（healthy 就正常）
docker compose logs -f               # 容器日志（只有 uvicorn 日志，任务日志不在这）
tail -f data/artifacts/run.log       # 真正要看的是这个：每轮任务的运行日志
cat data/artifacts/result.json       # 最近一轮结果
docker compose restart               # 重启
docker compose pull && docker compose up -d   # 用新镜像升级（如果你用 Docker Hub 镜像）
```

## 升级

用本地源码构建：

```bash
git pull
docker compose up -d --build
```

用 Docker Hub 镜像：把镜像 tag 换成新版本，`docker compose pull && docker compose up -d`。

## 备份 / 迁移

`data/` 目录打包带走即可（里面就是配置 + Cookie + 日志）：

```bash
tar czf douyin-backup-$(date +%F).tgz data/
```

迁移到新机器：装好 Docker → 解开 data/ → 写好 `.env` → `docker compose up -d`。

## 外网访问（可选）

容器只监听宿主机端口，默认内网可见。要从外网访问，务必：

1. 设置强 `WEB_PASSWORD`
2. 套反向代理并启用 HTTPS（Nginx / Caddy 示例）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8600;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

3. 别把 8600 直接暴露到公网（配置台能改 Cookie，等于账号控制权）

## 多账号

同一容器内支持多账号（`config/accounts.json` + 每账号一个 `.env.accountN`，详见原项目文档）。
但更简单稳妥的做法：**每个账号起一个容器**，各自挂不同的 `data/` 目录、用不同端口：

```bash
docker run -d --name dy-acc1 -p 8601:8600 -v $PWD/acc1:/data -e WEB_PASSWORD=pw1 caowenrui/douyin-auto-fire:latest
docker run -d --name dy-acc2 -p 8602:8600 -v $PWD/acc2:/data -e WEB_PASSWORD=pw2 caowenrui/douyin-auto-fire:latest
```

⚠️ 同一个抖音账号不要同时跑多个容器/任务。

## 时区

"每天几点发"用的是**容器内时间**，即 compose 里的 `TZ`（默认 `Asia/Shanghai`）。
配置台顶栏会显示服务器时间与下次运行时间，对一下就知道对不对。

## 资源占用

- 峰值内存：Chromium 打开视频页时约 300–600MB（`shm_size: 1gb` 已给足）
- 磁盘：镜像约 1.5GB；日志按天写 `data/artifacts/`，截图多了可以定期清
- 一轮任务耗时：每个好友 6–15 秒（视频分享比文字慢，因为要开视频页 + hover 分享 + 搜索好友）

## 排障清单

| 检查项 | 命令 |
| --- | --- |
| 容器活着吗 | `docker compose ps`（看 healthy） |
| 网页通吗 | `curl -s localhost:8600/healthz` |
| 登录态还有效吗 | 配置台点「测试登录」或在网页里跑 Dry Run |
| 上一轮发了什么 | `cat data/artifacts/result.json` |
| 为什么失败 | `tail -50 data/artifacts/run.log`，再看 `data/artifacts/screenshots/` 里的截图 |
