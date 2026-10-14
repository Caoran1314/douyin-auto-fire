# 🔥 抖音自动续火花 · Docker 版（自带配置网页 + 热门视频转发）

定时给抖音好友自动续火花。**Docker 一键部署，浏览器里点几下就能配好，还能自动转发热门视频**——会话里显示成正常的视频分享卡片，比只发文字自然得多（实测火花天数正常 +1）。

> 基于 [unmev/douyin-auto-fire](https://github.com/unmev/douyin-auto-fire) 二次开发（MIT，保留原作者版权）；
> 本次开发起点为 [cong120/douyin-auto-fire](https://github.com/cong120/douyin-auto-fire)。
> 本仓库主要新增：**容器化部署 + Web 配置台 + 每日晨报 + 私信列表扫描 + 热门视频转发**。

---

## ✨ 相比原项目新增了什么

| 功能 | 说明 |
| --- | --- |
| 🐳 **Docker 一键部署** | 一条 `docker compose up -d` 跑起来，内置 Chromium，不用装 Python 环境 |
| 🖥 **Web 配置台** | 自带密码保护的网页：改好友、改消息、换 Cookie、看日志、点按钮跑任务，不用碰命令行 |
| 🔎 **扫描私信列表** | 一键列出所有会话和**谁有火花**（含火花天数），点一下全导入 |
| 🎬 **按类型转发热门视频** | 视频池来自**你自己点过赞的「我的喜欢」+ 抖音热点榜**，按类型随机挑：美食介绍 / 旅游攻略 / 宠物猫狗 / 拍照相关 / 我的喜欢；每类的关键词在网页里可自己加减 |
| 🌅 **每日晨报** | 每天第一次运行给每个好友发一条定制晨报：`早上好 <昵称>` + 5 条热点新闻 + 按城市查的天气 + 一句名言；当天之后的运行只转发热门视频 |
| 🧪 **Dry Run 覆盖新链路** | 演练会真的打开视频页、hover 分享、在浮层里定位到好友，但**不点发送**；晨报会打印预览文本 |
| ⏰ **容器内定时** | 不用 systemd/cron，容器里自带调度器，每天固定时间跑 |

原本的能力也都在：多好友、多账号、文字/图片/抖音原生表情、防止重复发送、随机发送间隔、钉钉通知、失败截图与 Playwright Trace。

---

## 🚀 快速开始（Docker）

```bash
mkdir -p douyin-auto-fire && cd douyin-auto-fire
curl -fsSLO https://raw.githubusercontent.com/Caoran1314/douyin-auto-fire/main/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/Caoran1314/douyin-auto-fire/main/.env.example 2>/dev/null || true
# 没下到 .env.example 就自己建：
# echo "WEB_PASSWORD=换成你自己的密码" > .env

docker compose up -d
```

然后打开 **http://NAS或服务器IP:8600**，输入 `WEB_PASSWORD` 进入配置台。

也可以在仓库目录里直接构建本地镜像：

```bash
git clone https://github.com/Caoran1314/douyin-auto-fire.git && cd douyin-auto-fire
WEB_PASSWORD=yourpass docker compose -f deploy/docker/docker-compose.deploy.yml up -d --build
```

直接用现成镜像（Docker Hub）：

```bash
docker run -d --name douyin-auto-fire --restart unless-stopped \
  -p 8600:8600 -v $PWD/data:/data \
  -e WEB_PASSWORD=你的密码 -e TZ=Asia/Shanghai \
  --shm-size=1g caowenrui/douyin-auto-fire:latest
```

> Chromium 建议给 1GB 以上共享内存（compose 里已配 `shm_size: 1gb`），内存建议 ≥1GB。

---

## 🖥 配置台使用流程（5 步）

1. **拿 Cookie**：电脑浏览器登录 https://www.douyin.com/ → 装 Cookie-Editor 插件 → Export → 选 **JSON** → 复制整段
2. 打开配置台 →「③ 凭证与定时」→ 粘贴 Cookie → **保存 Cookie**
3. 「① 好友与消息」→ **扫描私信列表（看谁有火花）** → **一键导入有火花的好友**
4. 「③ 视频分享」→ 打开视频分享开关、勾选要发的**视频类型**（美食介绍 / 旅游攻略 / 宠物猫狗 / 拍照相关 / 我的喜欢），点「拉取视频池」能看到每类抓到多少条、样例是什么；关键词可自己加减
5. 「② 每日晨报」→ 设定问候语、热点条数、是否带天气/名言，并**给每个好友填一个城市**（天气按城市查；抖音网页版不暴露好友 IP/属地，所以只能手填，留空则那条晨报不带天气）
6. 回到任意页点底部 **💾 保存配置**

> **视频池怎么来的**：程序打开抖音网页版，抓「我的 / 喜欢」列表和「热点榜」，再按每类的关键词（标题/话题文字）分类。
> 抖音搜索页在自动化环境下不渲染（无头浏览器里 DOM 是空的），所以是"抓你自己的喜欢 + 热榜 → 按关键词归类"，关键词可自己调，比较像什么就加什么。

**发送规则**（这是本项目的核心逻辑）：

- **每天第一次运行**（自动 08:00 或你手动点「立即执行」）→ 每个好友收到一条**晨报**
- **当天之后的运行** → 只**转发热门视频**（「立即执行」会自动判断，也可直接点「只转发视频」）
- 底部还有「强制补发晨报」：忽略"今天已发过"，再发一遍晨报（补发/改完城市想立刻重发时用）

| 按钮 | 作用 |
| --- | --- |
| 扫描私信列表 | 只读：抓会话列表 + 火花天数，**不发送任何消息** |
| Dry Run | 演练：验证登录、找到好友、视频分享浮层能定位到好友，并预览晨报文本，**不发送** |
| 立即执行 | 真的发：当天首次=晨报，之后=只转发视频 |
| 只转发视频 | 强制只转发视频 |
| 强制补发晨报 | 强制再发一遍今天的晨报 |

---

## ⚙️ 配置说明（`/data/config.json`，建议都用网页改）

```jsonc
{
  "timezone": "Asia/Shanghai",
  "targets": [
    {
      "name": "好友昵称",              // 必须和抖音私信列表里的昵称完全一致
      "messages": [
        // random：从 choices 里随机挑一个；也可只写一个 text / image / douyin_sticker / video_share
        { "type": "random", "choices": [
          { "type": "text", "content": "续火花 ✨" },
          { "type": "video_share" }     // 转发热门视频
        ]}
      ]
    }
  ],
  "message_pool": ["续火花 ✨", "今天也要开心 😊"],
  "video_share": {
    "enabled": true,
    "ratio": 0.6,          // 转视频的概率（0~1）
    "per_friend": 1,       // 每个好友每次发几条
    "source": "hot",       // hot=抖音热点榜自动挑；list=用下面的 urls
    "urls": [],            // source=list 时的视频链接池
    "exclude": []          // 不想发的视频链接（热点榜里勾掉的就进这里）
  },
  "send_interval_seconds": { "min": 3, "max": 8 },
  "prevent_duplicates": false,   // true=同一天同一好友只发一次（失败后当天不会重试，谨慎开）
  "schedule": { "enabled": true, "time": "08:00" }   // 容器内定时，服务器本地时区（TZ）
}
```

发图片：把图片放到 `/data/` 下，用 `{"type": "image", "path": "images/a.jpg"}`。
发抖音原生表情：`{"type": "douyin_sticker", "sticker": "比心"}`，并在 `stickers` 里定义（见 `config/stickers.json`）。

环境变量（compose/命令行传入）：

| 变量 | 说明 |
| --- | --- |
| `WEB_PASSWORD` | 配置台访问密码（**强烈建议设置**，不设则任何人可访问） |
| `TZ` | 时区，影响"每天几点发"，默认 `Asia/Shanghai` |
| `WEB_PORT` | 网页端口，默认 `8600` |
| `DATA_DIR` | 数据目录，默认 `/data`（config.json / secrets/cookie.json / artifacts 都在这里） |

---

## 🔐 安全与隐私

- **Cookie 等于账号登录凭证**：只放在服务器本地（`/data/secrets/cookie.json`，权限 600），本仓库 `.gitignore` 已排除 `.env`、`config.json`、`secrets/`、`artifacts/`，**提交前请自行再确认一遍**
- **务必设置 `WEB_PASSWORD`**；不要把 8600 端口直接暴露到公网，需要外网访问请套反向代理 + HTTPS
- 抖音有风控：`send_interval_seconds.min` 别小于 3 秒；同一个账号不要同时跑多处
- 日志/截图里好友昵称会被自动打码成"好友01"，但**失败截图包含聊天画面**，别随手公开

---

## 🧰 其他部署方式

- GitHub Actions：见 `docs/github-actions.md`（**本仓库默认关掉了定时**，只留手动触发，避免没配 Secrets 时每天报失败）
- Linux 服务器 + systemd：见 `docs/server.md`
- Windows 本机：见 `docs/windows.md`

---

## ❓ 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 日志出现 `抖音登录状态已失效` / `要求进行安全验证` | Cookie 过期或触发风控：重新登录抖音网页版，导出新 Cookie 粘到配置台 |
| 找不到好友 | 昵称必须和私信列表完全一致（含 emoji、后缀）。用「扫描私信列表」看准确名字 |
| 视频分享失败 `分享浮层里找不到好友` | 该好友不在分享浮层的候选里：先确认昵称一致；分享浮层只有互关/近期好友，冷门好友可能搜不到 |
| 视频分享失败 `分享浮层没有出现` | 抖音前端改版了：分享图标 `data-e2e="video-share-icon-container"` 或浮层结构变了，需要按 `app/video_share.py` 里的选择器更新 |
| 页面能开但一片空白/按钮没反应 | 浏览器 console 有 JS 报错；本项目模板的 JS 一旦有语法错整页不渲染 |
| 手机/外网打不开配置台 | 只监听在服务器上，需内网访问或自己做端口转发 |

---

## ⚠️ 免责声明

本项目仅供学习与个人自动化研究使用。使用者应自行遵守抖音的用户协议与相关法律法规；因使用本工具导致的账号异常、封禁、数据或人际关系问题，由使用者自行承担，作者不承担任何责任。
