"""测试: 用户管理按序号(id)排序, 不按用户名首字母."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir


class TestUserListOrder(unittest.TestCase):
    """`list_users()` 必须按 id 升序(与页面上的 #编号 对得上)。"""

    def _store(self, root: Path):
        from dddb.public.users import UserStore

        return UserStore(str(root / "u.sqlite3"), iterations=500)

    def test_sorted_by_id(self) -> None:
        ctx = temp_dir("usersort-")
        root = Path(ctx.__enter__())
        try:
            store = self._store(root)
            # 故意让名字字母序与 id 序相反
            for name in ("zebra", "apple", "mango", "banana"):
                ok, msg = store.create_user(name, "password-123")
                self.assertTrue(ok, msg)
            users = store.list_users()
            ids = [u["id"] for u in users]
            self.assertEqual(ids, sorted(ids), "必须按 id 升序")
            store.close()
        finally:
            ctx.__exit__(None, None, None)

    def test_not_sorted_by_username(self) -> None:
        """确认没有退回按名字排序。"""
        ctx = temp_dir("usersort2-")
        root = Path(ctx.__enter__())
        try:
            store = self._store(root)
            for name in ("zebra", "apple", "mango"):
                store.create_user(name, "password-123")
            users = store.list_users()
            names = [u["username"] for u in users]
            self.assertNotEqual(names, sorted(names),
                                "按名字排的话这里会相等")
            self.assertEqual(names, ["zebra", "apple", "mango"],
                             "顺序应是创建顺序(id 序)")
            store.close()
        finally:
            ctx.__exit__(None, None, None)

    def test_id_ascending_across_gaps(self) -> None:
        """删号后 id 有空洞, 仍要按 id 升序而不是按名字。"""
        ctx = temp_dir("usersort3-")
        root = Path(ctx.__enter__())
        try:
            store = self._store(root)
            created = []
            for name in ("charlie", "alpha", "bravo", "delta"):
                store.create_user(name, "password-123")
                created.append(name)
            # 删掉第 2 个(alpha), 留下空洞
            store.remove("alpha")
            users = store.list_users()
            ids = [u["id"] for u in users]
            self.assertEqual(ids, sorted(ids))
            self.assertEqual([u["username"] for u in users],
                             ["charlie", "bravo", "delta"])
            store.close()
        finally:
            ctx.__exit__(None, None, None)

    def test_sql_uses_order_by_id(self) -> None:
        """源码层面守卫: list_users 的 SQL 必须 ORDER BY id。"""
        root = Path(__file__).resolve().parents[1]
        text = (root / "dddb/public/users.py").read_text(encoding="utf-8")
        start = text.find("def list_users(")
        self.assertGreater(start, 0)
        end = text.find("\n    def ", start + 10)
        block = text[start:end if end > 0 else start + 600]
        self.assertIn("ORDER BY id", block,
                      "list_users 必须 ORDER BY id")
        self.assertNotIn("ORDER BY username", block,
                         "不该再按用户名排序")


class TestAdminPageRendersInIdOrder(unittest.TestCase):
    """用户管理页面上渲染顺序也是 id 序。"""

    def test_page_order(self) -> None:
        import re

        from dddb.public import templates as T

        ctx = T.SiteContext(
            csrf_token="t", is_superuser=True, can_review=True,
            user={"id": 1, "username": "root", "role": "superuser"})
        users = [
            {"id": 2, "username": "zebra", "role": "viewer",
             "role_label": "普通用户", "is_active": True},
            {"id": 3, "username": "apple", "role": "viewer",
             "role_label": "普通用户", "is_active": True},
        ]
        html = T.users_admin_page(
            ctx, users=users,
            counts={"total": 2, "editors": 0, "pending": 0},
            error="", notice="")
        pos_zebra = html.find("zebra")
        pos_apple = html.find("apple")
        self.assertGreater(pos_zebra, 0)
        self.assertGreater(pos_apple, 0)
        self.assertLess(pos_zebra, pos_apple,
                        "页面按传入顺序渲染: zebra(#2) 在 apple(#3) 前")


if __name__ == "__main__":
    unittest.main()
