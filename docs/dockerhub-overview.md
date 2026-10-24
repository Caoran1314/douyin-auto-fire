# douyin-auto-fire

定时给抖音好友自动续火花。**Docker 一键部署，浏览器里点几下配置好**，还支持每日晨报与热门视频转发（会话里显示为正常视频分享卡片）。

本仓库只存放镜像。完整说明、截图与配置文档请看 GitHub：
**<https://github.com/Caoran1314/douyin-auto-fire>**

---

## 支持的标签

| 标签 | 说明 |
|---|---|
| `latest` | main 分支最新构建 |
| `v1.2.3` 等 | 固定版本，不随更新变化 |

架构：`linux/amd64`、`linux/arm64`。

## 快速开始

```bash
docker run -d --name douyin-auto-fire \
  -p 8080:8080 \
  -v /path/to/data:/app/data \
  --restart unless-stopped \
  caowenrui/douyin-auto-fire:latest
```

打开 `http://<IP>:8080`，在网页里扫码登录抖音、设置好友与时间即可。

## 说明

- 配置、Cookie、日志都保存在挂载的 `/app/data`，容器重建不丢
- 基于 [unmev/douyin-auto-fire](https://github.com/unmev/douyin-auto-fire) 二次开发（MIT）
- 仅供个人学习与自用，请遵守平台规则

## 相关链接

- 源码与文档：<https://github.com/Caoran1314/douyin-auto-fire>
- 详细部署说明：<https://github.com/Caoran1314/douyin-auto-fire/blob/main/docs/docker.md>
