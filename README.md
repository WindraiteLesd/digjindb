# DDDB — DoujinDigDatabase

同人音乐专辑数据库的**线上云端部分**。纯 Python 3.12 标准库实现，零第三方依赖。

线上实例：https://www.digjindb.top

> 本仓库只包含对外服务端（只读浏览 + 编辑端）。
> 数据采集/入库流水线、站长端管理面板及其通信部分不在本仓库内。

## 功能

### 浏览

- 专辑列表：关键词、社团、展会、标签、年份、语种、曲风多维筛选
- 专辑详情：封面、曲目表（多碟）、制作人员、简介、外链、评论
- 社团 / 展会 / 艺术家 / 标签索引页，全局看板
- 三语界面：中文 / English / 日本語（`?lang=` 或 cookie）

### 编辑端（需编辑者权限）

- 卡片式编辑页：封面 & 标题 / 出品信息 / 分类 / 简介 / 制作人员 / 外链
- **制作人员按钮**：有职责显示「职责 · 人名」，纯艺人显示「艺人 · 人名」；
  详情页与编辑页使用同一套合并算法，两边完全一致
- **多图封面槽位**：第 1 格固定是封面，右侧加号追加，每格可删；
  删掉封面自动把下一张提为封面
- **外链嵌入播放器**：支持粘贴 YouTube / SoundCloud 的 `<iframe>` embedding code，
  播放器渲染在链接按钮下方
- 草稿自动保存（localStorage）、修订历史（同一人短时间多次修改折叠）、
  单字段 / 整版还原

### 用户系统

- 注册（邮箱验证 + 邀请码可选）、登录、找回密码
- 关注社团 / 艺人 / 展会，评论、反馈、通知
- 角色：普通用户只读，编辑者可编辑

## 数据与合规

本站收录的是**第三方与用户共同提供**的专辑元数据，仅供参考。

- 公开页面**不出现数据来源站名**：外链一律走站内 `/link/<专辑>/<序号>` 中转
- 被禁止外跳的站点名单在 `dnh/linkpolicy.py`
- 嵌入播放器只接受 http(s) 的 iframe `src`，同样过这份名单
- 封面一律本站缓存分发，不 hotlink 源站
- `robots.txt` / `sitemap.xml` 自动生成，编辑/账号/审核路径不允许抓取

数据与曲目等内容的权利归各发布者 / 社团所有。

## 目录结构

```
dnh/
  public/          对外站点
    site.py        路由 + HTTP 服务 + 安全头
    templates.py   全部 HTML 模板（纯函数）
    data.py        只读数据视图层
    embed.py       外链 embedding code 解析
    users.py       用户/会话/邀请码
    profile.py     个人资料、收藏、关注、评论、反馈
    auth.py        CSRF / 登录态 / 权限
    ratelimit.py   令牌桶限速
    i18n.py        三语文案
    timefmt.py     按语言时区格式化时间
    static/        CSS / JS / 图标
  db.py            数据库（专辑库只读打开）
  models.py        Album / Track / Credit 模型
  linkpolicy.py    外链合规策略
  events.py        展会名解析
  config.py        配置加载
  cli.py           命令行入口（public 子命令）
tests/             单元测试
config/            配置样例
```

## 测试

```bash
python -m unittest discover -s tests -v
```

## 许可

代码以 MIT 许可发布，见 `LICENSE`。
站点收录的数据遵循 CC BY-SA 4.0。
