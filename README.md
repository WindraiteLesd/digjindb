# doujindigdb (dddb)

同人音乐专辑数据库网站。纯 Python 3.12 标准库实现，零第三方依赖。

线上实例：https://www.digjindb.top

本项目是一个**独立的数据库网站**：收录同人音乐专辑的元数据（标题、社团、曲目、
制作人员、发售信息、试听链接），提供浏览、检索与社区功能。数据由编辑者维护。

## 功能

### 浏览

- 专辑列表：关键词、社团、展会、标签、年份、语种、曲风多维筛选
- 专辑详情：封面、曲目表（多碟）、制作人员、简介、外链、评论
- 社团 / 展会 / 艺术家 / 标签索引页，全局看板
- 三语界面：中文 / English / 日本語
- 手机端卡片式布局（曲目表在窄屏重排为两行卡片，信息不丢）

### 编辑端（需编辑者权限）

- 卡片式编辑页：封面 & 标题 / 出品信息 / 分类 / 简介 / 制作人员 / 外链
- **制作人员按钮**：有职责显示「职责 · 人名」，纯艺人显示「艺人 · 人名」；
  详情页与编辑页使用同一套合并算法，两边完全一致
- **多图封面槽位**：第 1 格固定是封面，右侧加号追加，每格可删；
  删掉封面自动把下一张提为封面
- **外链嵌入播放器**：粘贴 YouTube / SoundCloud 的 `<iframe>` 代码即可内嵌播放，
  播放器在链接按钮下方；YouTube 按 16:9 自适应
- 草稿自动保存、修订历史（同一人短时间多次修改折叠）、单字段 / 整版还原

### 用户系统

- 注册（邮箱验证 + 邀请码）、登录、找回密码
- 关注社团 / 艺人 / 展会，评论、反馈、通知
- 角色：普通用户只读，编辑者可编辑
- 个人资料、收藏、隐私设置、外观主题

## 部署

### 要求

- Python 3.12+（仅标准库，无需安装依赖）
- 一个可写的 `data/` 目录

### 启动

```bash
python -m dnh public --root . --host 127.0.0.1 --port 8940
```

参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--root` | 当前目录 | 站点根目录（含 `dnh/` 与 `data/`） |
| `--db` | `<root>/data/dnh.sqlite3` | 专辑库（**只读打开**） |
| `--users-db` | `<root>/data/public_users.sqlite3` | 用户库 |
| `--sessions-db` | `<root>/data/sessions.sqlite3` | 会话库 |
| `--covers-dir` | `<root>/data/covers` | 封面缓存目录 |
| `--base-url` | 空 | 对外地址，写进 `robots.txt` / `sitemap.xml` |
| `--no-session-persist` | 关 | 会话只放内存，重启掉线（仅调试） |

### 反向代理

站点默认只监听 `127.0.0.1`。对外建议用 Caddy / Nginx 做 TLS 终止：

```caddy
www.example.com {
    reverse_proxy 127.0.0.1:8940
}
```

### 邮件（注册验证）

验证邮件通过环境变量配置的 HTTP 邮件服务发送，见 `config/.env.example`。
没配也能运行，只是验证邮件发不出去。

## 数据

`data/dnh.sqlite3` 是专辑库。数据库为空的站点可以启动，但列表与详情页都是空的。

站点通过编辑端维护数据：任何有权编辑的用户都可以在 `/album/<key>/edit`
修改专辑信息，每次保存都会留一条修订记录。

## 目录结构

```
dnh/
  public/          站点
    site.py        路由 + HTTP 服务 + 安全头
    templates.py   全部 HTML 模板（纯函数）
    data.py        只读数据视图层
    embed.py       外链 embedding code 解析
    users.py       用户 / 会话 / 邀请码
    profile.py     个人资料、收藏、关注、评论、反馈
    auth.py        CSRF / 登录态 / 权限
    ratelimit.py   令牌桶限速
    i18n.py        三语文案
    timefmt.py     按语言时区格式化时间
    static/        CSS / JS / 图标
  db.py            数据库（专辑库只读打开）
  models.py        Album / Track / Credit 模型
  events.py        展会名解析
  tagrules.py      标签清洗规则
  textutil.py      文本归一化
  config.py        配置加载
  cli.py           命令行入口
tests/             单元测试
config/            配置样例
```

## 技术要点

- **纯标准库**：HTTP 服务、HTML 模板、JSON、SHA-256、CSRF、限速全部自己实现，
  部署时不需要 `pip install`
- **只读数据层**：专辑库以 `file:...?mode=ro` 打开，写语句直接抛异常；
  编辑写入走独立的可写连接并做 CSRF + 同源校验
- **无内联脚本**：`Content-Security-Policy` 为 `script-src 'self'`，
  所有交互走 `data-*` 属性 + 事件委托
- **服务端渲染**：没有前端框架，页面首屏即可用，弱网/低端机也能看

## 测试

```bash
python -m unittest discover -s tests -t .
```

## 许可

代码以 MIT 许可发布，见 `LICENSE`。
站点收录的数据遵循 CC BY-SA 4.0。
