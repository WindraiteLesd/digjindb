"""轻量 i18n: 中文 / English / 日本語.

**逐步推进**: 目前覆盖导航、常用按钮、核心区块标题; 新文案按需加进 ``STRINGS``。
``translate(key, lang)`` 找不到就回退中文, 再回退调用方给的默认值。
"""

from __future__ import annotations

import re

#: 支持的语言 -> 显示名
LANGS: dict[str, str] = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
}

#: HTML ``lang`` 属性
HTML_LANGS: dict[str, str] = {"zh": "zh-CN", "en": "en", "ja": "ja"}

#: 文案表: key -> {lang: text}
STRINGS: dict[str, dict[str, str]] = {
    "nav.albums": {"zh": "浏览", "en": "Albums", "ja": "アルバム"},
    "nav.events": {"zh": "展会", "en": "Events", "ja": "イベント"},
    "nav.circles": {"zh": "社团", "en": "Circles", "ja": "サークル"},
    "nav.artists": {"zh": "艺术家", "en": "Artists", "ja": "アーティスト"},
    "nav.tags": {"zh": "标签", "en": "Tags", "ja": "タグ"},
    "nav.stats": {"zh": "看板", "en": "Stats", "ja": "統計"},
    "nav.edit": {"zh": "工作队列", "en": "Queue", "ja": "作業キュー"},
    "nav.about": {"zh": "关于", "en": "About", "ja": "概要"},
    "nav.login": {"zh": "登录", "en": "Sign in", "ja": "ログイン"},
    "nav.register": {"zh": "注册", "en": "Sign up", "ja": "登録"},
    "nav.review": {"zh": "审核队列", "en": "Review", "ja": "レビュー"},
    "nav.users": {"zh": "用户管理", "en": "Users", "ja": "ユーザー"},
    "common.follow": {"zh": "关注", "en": "Follow", "ja": "フォロー"},
    "common.following": {"zh": "已关注", "en": "Following", "ja": "フォロー中"},
    "common.followers": {"zh": "关注者", "en": "Followers", "ja": "フォロワー"},
    "section.bio": {"zh": "简介", "en": "About", "ja": "紹介"},
    "section.albums": {"zh": "专辑", "en": "Albums", "ja": "アルバム"},
    "section.tracks": {"zh": "曲目表", "en": "Tracks", "ja": "トラック"},
    "section.credits": {"zh": "制作人员", "en": "Credits", "ja": "クレジット"},
    "section.comments": {"zh": "评论", "en": "Comments", "ja": "コメント"},
    "section.circles": {"zh": "相关社团", "en": "Related circles", "ja": "関連サークル"},
    "section.people": {"zh": "相关人士", "en": "People", "ja": "関係者"},
    "section.relations": {"zh": "人际关系", "en": "Relations", "ja": "関係"},
    "search.placeholder": {
        "zh": "搜索标题 / 社团 / 艺人 / 曲目 / 碟名 / 编号",
        "en": "Search title / circle / artist / track / disc / catalog",
        "ja": "タイトル / サークル / アーティスト / 曲名 / ディスク名 / 型番で検索",
    },
    "lang.label": {"zh": "语言", "en": "Language", "ja": "言語"},
}

#: 界面常用文案(第二批): 覆盖页面标题、按钮、区块标题、提示语
_EXTRA_STRINGS: dict[str, dict[str, str]] = {
    "page.home": {"zh": "首页", "en": "Home", "ja": "ホーム"},
    "page.albums": {"zh": "浏览专辑", "en": "Albums", "ja": "アルバム一覧"},
    "page.album": {"zh": "专辑", "en": "Album", "ja": "アルバム"},
    "page.circles": {"zh": "社团", "en": "Circles", "ja": "サークル"},
    "page.artists": {"zh": "艺术家", "en": "Artists", "ja": "アーティスト"},
    "page.events": {"zh": "展会", "en": "Events", "ja": "イベント"},
    "page.tags": {"zh": "标签", "en": "Tags", "ja": "タグ"},
    "page.stats": {"zh": "看板", "en": "Stats", "ja": "統計"},
    "page.about": {"zh": "关于", "en": "About", "ja": "概要"},
    "page.account": {"zh": "我的账号", "en": "My account", "ja": "マイアカウント"},
    "page.login": {"zh": "登录", "en": "Sign in", "ja": "ログイン"},
    "page.register": {"zh": "注册", "en": "Sign up", "ja": "新規登録"},
    "page.search": {"zh": "搜索", "en": "Search", "ja": "検索"},
    "section.tracks": {"zh": "曲目表", "en": "Tracklist", "ja": "トラックリスト"},
    "section.credits": {"zh": "制作人员", "en": "Credits", "ja": "クレジット"},
    "section.comments": {"zh": "评论", "en": "Comments", "ja": "コメント"},
    "section.description": {"zh": "简介", "en": "Description", "ja": "紹介"},
    "section.links": {"zh": "外链", "en": "Links", "ja": "リンク"},
    "section.related": {"zh": "同社团的其他专辑", "en": "More from this circle",
                        "ja": "同じサークルの作品"},
    "section.entities": {"zh": "艺人与社团", "en": "Artists & circles", "ja": "アーティストとサークル"},
    "section.people": {"zh": "相关人士", "en": "People", "ja": "関係者"},
    "section.former": {"zh": "过往成员", "en": "Former members", "ja": "過去のメンバー"},
    "section.guests": {"zh": "客串", "en": "Guests", "ja": "ゲスト"},
    "section.albums": {"zh": "专辑", "en": "Albums", "ja": "アルバム"},
    "section.relations": {"zh": "人际关系", "en": "Relations", "ja": "関係"},
    "section.appearance": {"zh": "外观设置", "en": "Appearance", "ja": "外観設定"},
    "section.privacy": {"zh": "隐私设置", "en": "Privacy", "ja": "プライバシー"},
    "section.security": {"zh": "安全设置", "en": "Security", "ja": "セキュリティ"},
    "field.title": {"zh": "标题", "en": "Title", "ja": "タイトル"},
    "field.circle": {"zh": "社团", "en": "Circle", "ja": "サークル"},
    "field.artists": {"zh": "艺人", "en": "Artists", "ja": "アーティスト"},
    "field.release_date": {"zh": "发售日", "en": "Release date", "ja": "発売日"},
    "field.event": {"zh": "展会", "en": "Event", "ja": "イベント"},
    "field.hall": {"zh": "展馆", "en": "Hall", "ja": "会場"},
    "field.booth": {"zh": "展位", "en": "Booth", "ja": "ブース"},
    "field.catalog_no": {"zh": "编号", "en": "Catalog no.", "ja": "型番"},
    "field.price": {"zh": "价格", "en": "Price", "ja": "価格"},
    "field.original_work": {"zh": "原作", "en": "Original work", "ja": "原作"},
    "field.producer": {"zh": "制作人", "en": "Producer", "ja": "プロデュース"},
    "field.platform": {"zh": "发布平台", "en": "Platform", "ja": "配信"},
    "field.tags": {"zh": "标签", "en": "Tags", "ja": "タグ"},
    "field.genres": {"zh": "曲风", "en": "Genres", "ja": "ジャンル"},
    "field.description": {"zh": "简介", "en": "Description", "ja": "紹介"},
    "field.name": {"zh": "名称", "en": "Name", "ja": "名前"},
    "field.real_name": {"zh": "本名", "en": "Real name", "ja": "本名"},
    "field.gender": {"zh": "性别", "en": "Gender", "ja": "性別"},
    "field.birthday": {"zh": "生日", "en": "Birthday", "ja": "誕生日"},
    "field.nationality": {"zh": "国籍", "en": "Nationality", "ja": "国籍"},
    "field.occupations": {"zh": "职业", "en": "Occupations", "ja": "職業"},
    "field.aliases": {"zh": "别名", "en": "Aliases", "ja": "別名"},
    "field.avatar": {"zh": "头像", "en": "Avatar", "ja": "アイコン"},
    "field.cover": {"zh": "封面", "en": "Cover", "ja": "ジャケット"},
    "btn.save": {"zh": "保存", "en": "Save", "ja": "保存"},
    "btn.cancel": {"zh": "取消", "en": "Cancel", "ja": "キャンセル"},
    "btn.create": {"zh": "创建", "en": "Create", "ja": "作成"},
    "btn.edit": {"zh": "编辑", "en": "Edit", "ja": "編集"},
    "btn.delete": {"zh": "删除", "en": "Delete", "ja": "削除"},
    "btn.follow": {"zh": "关注", "en": "Follow", "ja": "フォロー"},
    "btn.following": {"zh": "已关注", "en": "Following", "ja": "フォロー中"},
    "btn.logout": {"zh": "登出", "en": "Sign out", "ja": "ログアウト"},
    "btn.upload": {"zh": "上传", "en": "Upload", "ja": "アップロード"},
    "btn.more": {"zh": "更多", "en": "More", "ja": "もっと見る"},
    "msg.empty": {"zh": "暂无内容", "en": "Nothing here yet", "ja": "まだ何もありません"},
    "msg.login_required": {"zh": "请先登录", "en": "Please sign in first", "ja": "先にログインしてください"},
    "msg.saved": {"zh": "已保存", "en": "Saved", "ja": "保存しました"},
    "msg.created": {"zh": "已创建", "en": "Created", "ja": "作成しました"},
}

STRINGS.update(_EXTRA_STRINGS)

#: 界面文案(第三批): 表单字段 / 账号页 / 编辑与新建 / 详情页 / 提示
_BATCH3: dict[str, dict[str, str]] = {
    # 表单字段
    "field.title_original": {"zh": "原标题", "en": "Original title", "ja": "原題"},
    "field.title_romaji": {"zh": "罗马音", "en": "Romaji", "ja": "ローマ字"},
    "field.name_en": {"zh": "艺名(英)", "en": "Name (EN)", "ja": "名前（英）"},
    "field.name_ja": {"zh": "艺名(日)", "en": "Name (JA)", "ja": "名前（日）"},
    "field.real_name_en": {"zh": "本名(译)", "en": "Real name (translated)",
                           "ja": "本名（訳）"},
    "field.occupations_text": {"zh": "职业", "en": "Occupations", "ja": "職業"},
    "field.activity_type": {"zh": "活动类型", "en": "Activity type", "ja": "活動種別"},
    "field.founded": {"zh": "成立时间", "en": "Founded", "ja": "設立"},
    "field.last_active": {"zh": "最后活动", "en": "Last active", "ja": "最終活動"},
    "field.start_date": {"zh": "开始日期", "en": "Start date", "ja": "開始日"},
    "field.end_date": {"zh": "结束日期", "en": "End date", "ja": "終了日"},
    "field.year": {"zh": "年份", "en": "Year", "ja": "年"},
    "field.season": {"zh": "季节", "en": "Season", "ja": "季節"},
    "field.kind": {"zh": "类型", "en": "Type", "ja": "種別"},
    "field.note": {"zh": "说明", "en": "Note", "ja": "メモ"},
    "field.role": {"zh": "角色", "en": "Role", "ja": "役割"},
    "field.tracks": {"zh": "曲目", "en": "Tracks", "ja": "トラック"},
    "field.credits": {"zh": "制作人员", "en": "Credits", "ja": "クレジット"},
    "field.urls": {"zh": "外链", "en": "Links", "ja": "リンク"},
    "field.staff": {"zh": "成员", "en": "Members", "ja": "メンバー"},
    "field.code": {"zh": "验证码", "en": "Verification code", "ja": "認証コード"},
    "field.new_password": {"zh": "新密码", "en": "New password", "ja": "新しいパスワード"},
    "field.new_password2": {"zh": "确认新密码", "en": "Confirm password",
                            "ja": "パスワード（確認）"},
    "field.username": {"zh": "用户名", "en": "Username", "ja": "ユーザー名"},
    "field.password": {"zh": "密码", "en": "Password", "ja": "パスワード"},
    "field.email": {"zh": "邮箱", "en": "Email", "ja": "メールアドレス"},
    "field.bio": {"zh": "简介", "en": "Bio", "ja": "自己紹介"},
    "field.contact": {"zh": "联系方式", "en": "Contact", "ja": "連絡先"},
    "field.reason": {"zh": "申请理由", "en": "Reason", "ja": "申請理由"},
    "field.body": {"zh": "内容", "en": "Content", "ja": "内容"},
    "field.subject": {"zh": "主题", "en": "Subject", "ja": "件名"},
    # 提示
    "hint.required": {"zh": "必填", "en": "required", "ja": "必須"},
    "hint.optional": {"zh": "选填", "en": "optional", "ja": "任意"},
    "hint.per_line": {"zh": "每行一条", "en": "one per line", "ja": "1行に1件"},
    "hint.enter_to_split": {"zh": "回车分隔多个", "en": "press Enter to add",
                            "ja": "Enter で区切る"},
    "hint.comma_split": {"zh": "逗号分隔", "en": "comma separated", "ja": "カンマ区切り"},
    "hint.date_format": {"zh": "YYYY-MM-DD", "en": "YYYY-MM-DD", "ja": "YYYY-MM-DD"},
    # 账号页
    "account.my": {"zh": "我的账号", "en": "My account", "ja": "マイアカウント"},
    "account.overview": {"zh": "总览", "en": "Overview", "ja": "概要"},
    "account.profile": {"zh": "资料", "en": "Profile", "ja": "プロフィール"},
    "account.favorites": {"zh": "收藏", "en": "Favorites", "ja": "お気に入り"},
    "account.follows": {"zh": "关注", "en": "Following", "ja": "フォロー"},
    "account.comments": {"zh": "评论", "en": "Comments", "ja": "コメント"},
    "account.contributions": {"zh": "贡献", "en": "Contributions", "ja": "貢献"},
    "account.messages": {"zh": "私信", "en": "Messages", "ja": "メッセージ"},
    "account.notifications": {"zh": "通知", "en": "Notifications", "ja": "通知"},
    "account.feedback": {"zh": "反馈", "en": "Feedback", "ja": "フィードバック"},
    "account.security": {"zh": "安全", "en": "Security", "ja": "セキュリティ"},
    "account.privacy": {"zh": "隐私", "en": "Privacy", "ja": "プライバシー"},
    "account.appearance": {"zh": "外观", "en": "Appearance", "ja": "外観"},
    "account.role_user": {"zh": "用户", "en": "User", "ja": "ユーザー"},
    "account.role_editor": {"zh": "编辑者", "en": "Editor", "ja": "編集者"},
    "account.role_admin": {"zh": "站务", "en": "Admin", "ja": "管理者"},
    "account.followers": {"zh": "关注者", "en": "Followers", "ja": "フォロワー"},
    "account.unread_messages": {"zh": "未读私信", "en": "Unread messages",
                                "ja": "未読メッセージ"},
    "account.unread_notifications": {"zh": "未读通知", "en": "Unread notifications",
                                     "ja": "未読通知"},
    # 编辑 / 新建
    "edit.album": {"zh": "编辑专辑", "en": "Edit album", "ja": "アルバムを編集"},
    "edit.profile": {"zh": "编辑资料", "en": "Edit profile", "ja": "プロフィール編集"},
    "edit.save": {"zh": "保存", "en": "Save", "ja": "保存"},
    "new.album": {"zh": "新建专辑", "en": "New album", "ja": "アルバム作成"},
    "new.circle": {"zh": "新建社团", "en": "New circle", "ja": "サークル作成"},
    "new.artist": {"zh": "新建艺人", "en": "New artist", "ja": "アーティスト作成"},
    "new.event": {"zh": "新建展会", "en": "New event", "ja": "イベント作成"},
    "new.tag": {"zh": "新建标签", "en": "New tag", "ja": "タグ作成"},
    "new.queue": {"zh": "工作队列", "en": "Work queue", "ja": "作業キュー"},
    # 详情页
    "detail.release_date": {"zh": "发售日", "en": "Release date", "ja": "発売日"},
    "detail.updated": {"zh": "更新时间", "en": "Updated", "ja": "更新日"},
    "detail.credits": {"zh": "制作人员", "en": "Credits", "ja": "クレジット"},
    "detail.related": {"zh": "同社团的其他专辑", "en": "More from this circle",
                       "ja": "同じサークルの作品"},
    "detail.no_tracks": {"zh": "这张专辑还没有曲目表。", "en": "No tracklist yet.",
                         "ja": "トラックリストはまだありません。"},
    "detail.no_description": {"zh": "暂无简介。", "en": "No description yet.",
                              "ja": "紹介文はまだありません。"},
    "detail.no_comments": {"zh": "还没有评论, 来写第一条吧。",
                           "en": "No comments yet. Be the first!",
                           "ja": "コメントはまだありません。"},
    "detail.no_albums": {"zh": "还没有参与专辑。", "en": "No albums yet.",
                         "ja": "参加作品はまだありません。"},
    "detail.first_event": {"zh": "首次活动", "en": "First event", "ja": "初参加"},
    "detail.last_event": {"zh": "末次活动", "en": "Latest event", "ja": "最新参加"},
    "detail.activity": {"zh": "活动状态", "en": "Activity", "ja": "活動状況"},
    # 通用
    "common.all": {"zh": "全部", "en": "All", "ja": "すべて"},
    "common.none": {"zh": "无", "en": "None", "ja": "なし"},
    "common.yes": {"zh": "是", "en": "Yes", "ja": "はい"},
    "common.no": {"zh": "否", "en": "No", "ja": "いいえ"},
    "common.total": {"zh": "共", "en": "Total", "ja": "合計"},
    "common.songs": {"zh": "首", "en": "tracks", "ja": "曲"},
    "common.albums": {"zh": "张专辑", "en": "albums", "ja": "枚"},
    "common.followers": {"zh": "关注者", "en": "followers", "ja": "フォロワー"},
    "common.search": {"zh": "搜索", "en": "Search", "ja": "検索"},
    "common.prev": {"zh": "上一页", "en": "Previous", "ja": "前へ"},
    "common.next": {"zh": "下一页", "en": "Next", "ja": "次へ"},
    "common.loading": {"zh": "加载中…", "en": "Loading…", "ja": "読み込み中…"},
    "common.copy": {"zh": "复制", "en": "Copy", "ja": "コピー"},
    "common.close": {"zh": "关闭", "en": "Close", "ja": "閉じる"},
    "common.confirm": {"zh": "确认", "en": "Confirm", "ja": "確認"},
    "common.back": {"zh": "返回", "en": "Back", "ja": "戻る"},
}

STRINGS.update(_BATCH3)

#: 界面文案(第四批): 按钮 / 提示 / 空状态 / 错误
_BATCH4: dict[str, dict[str, str]] = {
    "btn.search": {"zh": "搜索", "en": "Search", "ja": "検索"},
    "btn.submit": {"zh": "提交", "en": "Submit", "ja": "送信"},
    "btn.back": {"zh": "返回", "en": "Back", "ja": "戻る"},
    "btn.more": {"zh": "更多", "en": "More", "ja": "もっと見る"},
    "btn.expand": {"zh": "展开", "en": "Expand", "ja": "展開"},
    "btn.collapse": {"zh": "收起", "en": "Collapse", "ja": "折りたたむ"},
    "btn.upload": {"zh": "上传", "en": "Upload", "ja": "アップロード"},
    "btn.reset": {"zh": "恢复默认", "en": "Reset", "ja": "初期化"},
    "btn.apply": {"zh": "应用", "en": "Apply", "ja": "適用"},
    "btn.clear": {"zh": "清除", "en": "Clear", "ja": "クリア"},
    "empty.comments": {"zh": "还没有评论", "en": "No comments yet", "ja": "コメントなし"},
    "empty.albums": {"zh": "没有找到专辑", "en": "No albums found", "ja": "アルバムが見つかりません"},
    "empty.artists": {"zh": "没有找到艺术家", "en": "No artists found", "ja": "アーティストが見つかりません"},
    "empty.circles": {"zh": "没有找到社团", "en": "No circles found", "ja": "サークルが見つかりません"},
    "empty.tags": {"zh": "没有找到标签", "en": "No tags found", "ja": "タグが見つかりません"},
    "empty.events": {"zh": "没有找到展会", "en": "No events found", "ja": "イベントが見つかりません"},
    "empty.users": {"zh": "没有匹配的用户", "en": "No users found", "ja": "ユーザーが見つかりません"},
    "nav.artists": {"zh": "艺术家", "en": "Artists", "ja": "アーティスト"},
    "nav.login": {"zh": "登录", "en": "Sign in", "ja": "ログイン"},
    "nav.register": {"zh": "注册", "en": "Sign up", "ja": "新規登録"},
    "lang.zh": {"zh": "中文", "en": "Chinese", "ja": "中国語"},
    "lang.en": {"zh": "英文", "en": "English", "ja": "英語"},
    "lang.ja": {"zh": "日文", "en": "Japanese", "ja": "日本語"},
    "theme.dark": {"zh": "暗色", "en": "Dark", "ja": "ダーク"},
    "theme.light": {"zh": "明亮", "en": "Light", "ja": "ライト"},
    "theme.warm": {"zh": "暖色", "en": "Warm", "ja": "ウォーム"},
    "theme.contrast": {"zh": "高对比", "en": "High contrast", "ja": "ハイコントラスト"},
    "theme.forest": {"zh": "森林", "en": "Forest", "ja": "フォレスト"},
    "theme.sakura": {"zh": "樱花", "en": "Sakura", "ja": "サクラ"},
    "hint.background": {"zh": "背景图", "en": "Background", "ja": "背景"},
    "hint.overlay": {"zh": "遮罩", "en": "Overlay", "ja": "オーバーレイ"},
    "hint.blur": {"zh": "模糊", "en": "Blur", "ja": "ぼかし"},
    "hint.density": {"zh": "密度", "en": "Density", "ja": "密度"},
    "hint.radius": {"zh": "圆角", "en": "Radius", "ja": "角丸"},
    "hint.fontsize": {"zh": "字号", "en": "Font size", "ja": "文字サイズ"},
    "msg.merge_pick": {"zh": "至少选两张", "en": "Pick at least two",
                       "ja": "2つ以上選択してください"},
    "msg.merged": {"zh": "已合并", "en": "Merged", "ja": "統合しました"},
    "msg.imported": {"zh": "已导入", "en": "Imported", "ja": "インポートしました"},
    "msg.copied": {"zh": "已复制", "en": "Copied", "ja": "コピーしました"},
}

STRINGS.update(_BATCH4)

_LANG_RE = re.compile(r"^([a-zA-Z]{2})")


def language_of(value: object, default: str = "zh") -> str:
    """把 ``zh-CN`` / ``en_US`` / ``ja`` 等规整成受支持的两位代码."""
    match = _LANG_RE.match(str(value or "").strip())
    code = match.group(1).lower() if match else ""
    return code if code in LANGS else default


def translate(key: str, lang: object = "zh", default: str = "") -> str:
    """取文案: 目标语言 -> 中文 -> ``default`` -> key."""
    code = language_of(lang)
    entry = STRINGS.get(str(key))
    if not entry:
        return default or key
    return entry.get(code) or entry.get("zh") or default or key
