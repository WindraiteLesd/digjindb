# DoujinDigDatabase（digjindb）

**同人音乐展会专辑数据库** · https://www.digjindb.top

按展会（M3 / Comic Market / 例大祭…）整理同人音乐专辑，记录专辑、社团、
艺人、曲目、封面与发售信息，支持按展会/社团/年份/标签筛选与全文搜索。

纯 Python 标准库实现，**零第三方依赖**。

---

## 功能

- **专辑** —— 标题（原名/罗马音）、社团、艺人、发售日、展会、展位、
  编号、价格、曲目表、封面、制作人员、外链
- **展会** —— 两级结构：系列（缩写/全称/LOGO/wiki）+ 会次（`[M3] M3-2026秋 (M3-58)`）
- **社团 / 艺人** —— 独立词条，含成立时间、活动类型、成员、别名
- **搜索** —— 标题 / 社团 / 艺人 / 曲目 / 编号全文检索
- **用户** —— 注册、邮箱验证、收藏标记、评论、编辑审核队列
- **编辑** —— 编辑者可在网页上修改词条，改动留修订历史

---

## 运行

```bash
python -m dddb public --root . --host 127.0.0.1 --port 8940
```

首次运行前：

```bash
python -m dddb init       # 建目录与默认配置
```

配置文件 `config/config.toml`，站点相关的设置在 `[public]` 段。

### 部署

需要一个 WSGI 之外的最小 HTTP 服务即可；生产环境前面放反向代理（Caddy / Nginx）。

```toml
[public]
sessions_db = "data/sessions.sqlite3"
base_url = "https://www.digjindb.top"
behind_proxy = true
rate_limit_enabled = true
```

---

## 目录

```
dddb/
  public/           Web 层（页面、路由、模板、鉴权）
    site.py         路由与请求处理
    templates.py    页面模板
    data.py         只读数据访问
    auth.py         会话与 CSRF
    users.py        用户与权限
    static/         样式与脚本
  db.py             数据存储
  models.py         数据模型
  config.py         配置
tests/              测试
config/             配置样例
```

---

## 许可

见 [LICENSE](LICENSE)。
