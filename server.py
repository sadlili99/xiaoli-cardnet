#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小黎卡网 —— HTML + Python 后端 + SQLite 数据库（零第三方依赖，只用标准库）
启动：python3 server.py          端口：环境变量 PORT，默认 8088
数据：data/cardnet.db（自动建表 + 种子数据，重启不丢）
管理员：admin / admin123（首次登录后请到「我的 → 改密码」修改）
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, "public")
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "cardnet.db")
PORT = int(os.environ.get("PORT", "8088"))
os.makedirs(DATA_DIR, exist_ok=True)
LOCK = threading.Lock()
TOKEN_TTL = 30 * 24 * 3600


def now():
    return int(time.time())


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def money(x):
    return round(float(x) + 1e-9, 2)


def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def hash_pass(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return salt.hex() + "$" + h.hex()


def check_pass(pw, stored):
    try:
        salt_hex, h_hex = stored.split("$")
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1, dklen=64)
        return secrets.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      pass_hash TEXT NOT NULL, balance REAL NOT NULL DEFAULT 0,
      is_admin INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS categories(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER,
      name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '',
      price REAL NOT NULL, stock_mode TEXT NOT NULL DEFAULT 'auto',
      status INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS cards(
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
      code TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available',
      order_id INTEGER, sold_to INTEGER, sold_at INTEGER, created_at INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_cards_p ON cards(product_id,status);
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT UNIQUE NOT NULL,
      user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, product_name TEXT NOT NULL,
      unit_price REAL NOT NULL, quantity INTEGER NOT NULL, total_price REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'done', created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS wallet_logs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
      amount REAL NOT NULL, balance_after REAL NOT NULL, reason TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS announcements(
      id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS op_logs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
    """)
    if not con.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        con.execute("INSERT INTO users(username,pass_hash,balance,is_admin,status,created_at) VALUES('admin',?,0,1,1,?)",
                    (hash_pass("admin123"), now()))
        print("[seed] 管理员已创建：admin / admin123")
    if con.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"] == 0:
        cats = [("游戏点卡", 1), ("影视会员", 2), ("软件会员", 3), ("话费流量", 4)]
        con.executemany("INSERT INTO categories(name,sort) VALUES(?,?)", cats)
        cid = {r["name"]: r["id"] for r in con.execute("SELECT id,name FROM categories")}
        ps = [
            (cid["游戏点卡"], "Steam 钱包充值卡 30元", "Steam 国区钱包充值码，拍下自动发卡，复制去 Steam 兑换。", 28.5, "auto", 1),
            (cid["影视会员"], "某酷视频 VIP 月卡", "官方卡密，手机/电脑/平板都能用，兑换后查得到到期时间。", 12.0, "auto", 2),
            (cid["软件会员"], "WPS 超级会员月卡", "官方卡密兑换，含稻壳权益，兑换立即生效。", 9.9, "auto", 3),
            (cid["话费流量"], "全国移动 50元话费慢充", "人工发货，72 小时内到账，下单后留意订单状态。", 48.0, "manual", 4),
        ]
        t = now()
        for c, n, d, p, mode, s in ps:
            cur = con.execute("INSERT INTO products(category_id,name,description,price,stock_mode,status,sort,created_at) VALUES(?,?,?,?,?,1,?,?)",
                              (c, n, d, p, mode, s, t))
            pid = cur.lastrowid
            if mode == "auto":
                for k in range(1, 6):
                    con.execute("INSERT INTO cards(product_id,code,status,created_at) VALUES(?,?,'available',?)",
                                (pid, "DEMO-%02d%d-%s" % (pid, k, secrets.token_hex(3).upper()), t))
        con.execute("INSERT INTO announcements(content,created_at) VALUES(?,?)",
                    ("下单自动发卡，卡密在订单里随时能查；话费是人工发货，别催，72 小时内到。", t))
        print("[seed] 示例分类/商品/卡密已写入（DEMO- 开头，上线前在后台删掉）")
    con.commit()
    con.close()


def stock_of(con, pid):
    return con.execute("SELECT COUNT(*) c FROM cards WHERE product_id=? AND status='available'", (pid,)).fetchone()["c"]


def op_log(con, actor, action, detail=""):
    con.execute("INSERT INTO op_logs(actor,action,detail,created_at) VALUES(?,?,?,?)",
                (actor, action, str(detail)[:500], now()))


class ApiError(Exception):
    def __init__(self, msg, code=400):
        self.msg, self.code = msg, code


class Handler(BaseHTTPRequestHandler):
    server_version = "CardNetPy/1.0"

    def log_message(self, *a):
        pass

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def ok(self, data=None):
        self.send_json(dict(ok=True, **(data or {})))

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            raise ApiError("请求体不是合法 JSON")

    def auth(self, con):
        m = re.match(r"Bearer\s+([0-9a-f]{64})", self.headers.get("Authorization") or "")
        if not m:
            return None
        s = con.execute("SELECT * FROM sessions WHERE token=?", (m.group(1),)).fetchone()
        if not s or s["expires_at"] < now():
            return None
        u = con.execute("SELECT id,username,balance,is_admin,status,created_at FROM users WHERE id=?", (s["user_id"],)).fetchone()
        return u if u and u["status"] == 1 else None

    def need_login(self, con):
        u = self.auth(con)
        if not u:
            raise ApiError("登录已过期，请重新登录", 401)
        return u

    def need_admin(self, con):
        u = self.need_login(con)
        if not u["is_admin"]:
            raise ApiError("需要管理员权限", 403)
        return u

    # ---------- routing ----------
    def do_GET(self):
        self.handle_all("GET")

    def do_POST(self):
        self.handle_all("POST")

    def do_PUT(self):
        self.handle_all("PUT")

    def do_DELETE(self):
        self.handle_all("DELETE")

    def handle_all(self, method):
        try:
            u = urlparse(self.path)
            if u.path.startswith("/api/"):
                self.route(method, u.path, parse_qs(u.query))
            else:
                self.static(u.path)
        except ApiError as e:
            self.send_json(dict(ok=False, msg=e.msg), e.code)
        except Exception as e:
            print("[error]", repr(e))
            self.send_json(dict(ok=False, msg="服务器内部错误：" + str(e)), 500)

    def static(self, rel):
        if rel == "/":
            rel = "/index.html"
        fp = os.path.normpath(os.path.join(PUBLIC, unquote(rel).lstrip("/")))
        if not fp.startswith(PUBLIC) or not os.path.isfile(fp):
            idx = os.path.join(PUBLIC, "index.html")
            if os.path.isfile(idx):
                fp = idx
            else:
                self.send_response(404); self.end_headers(); self.wfile.write(b"Not found"); return
        mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8", ".png": "image/png",
                ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        data = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", mime.get(os.path.splitext(fp)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self, m, p, q):
        con = db()
        try:
            self.api(con, m, p, q)
        finally:
            con.close()

    def api(self, con, m, p, q):
        # ---------- 公开 ----------
        if p == "/api/announcements" and m == "GET":
            rows = con.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 5").fetchall()
            return self.ok(dict(list=[dict(id=r["id"], content=r["content"], time=fmt_time(r["created_at"])) for r in rows]))
        if p == "/api/categories" and m == "GET":
            rows = con.execute("SELECT id,name FROM categories ORDER BY sort,id").fetchall()
            return self.ok(dict(list=[dict(r) for r in rows]))
        if p == "/api/products" and m == "GET":
            sql = """SELECT p.*, c.name category_name FROM products p
                     LEFT JOIN categories c ON c.id=p.category_id WHERE p.status=1"""
            args = []
            if q.get("category", [""])[0]:
                sql += " AND p.category_id=?"; args.append(int(q["category"][0]))
            if q.get("q", [""])[0].strip():
                sql += " AND p.name LIKE ?"; args.append("%" + q["q"][0].strip() + "%")
            sql += " ORDER BY p.sort,p.id"
            out = []
            for r in con.execute(sql, args):
                out.append(dict(id=r["id"], category_id=r["category_id"], category_name=r["category_name"],
                                name=r["name"], description=r["description"], cover=r["cover"], price=r["price"],
                                stock_mode=r["stock_mode"], stock=stock_of(con, r["id"])))
            return self.ok(dict(list=out))
        pm = re.match(r"^/api/products/(\d+)$", p)
        if pm and m == "GET":
            r = con.execute("""SELECT p.*, c.name category_name FROM products p
                               LEFT JOIN categories c ON c.id=p.category_id WHERE p.id=? AND p.status=1""",
                            (int(pm.group(1)),)).fetchone()
            if not r:
                raise ApiError("商品不存在或已下架", 404)
            return self.ok(dict(product=dict(id=r["id"], category_id=r["category_id"], category_name=r["category_name"],
                                             name=r["name"], description=r["description"], cover=r["cover"], price=r["price"],
                                             stock_mode=r["stock_mode"], stock=stock_of(con, r["id"]))))
        if p == "/api/order-query" and m == "POST":
            b = self.body()
            no = str(b.get("order_no") or "").strip().upper()
            if not no:
                raise ApiError("请输入订单号")
            o = con.execute("SELECT * FROM orders WHERE order_no=?", (no,)).fetchone()
            if not o:
                raise ApiError("订单不存在，请核对订单号", 404)
            cards = [r["code"] for r in con.execute("SELECT code FROM cards WHERE order_id=? ORDER BY id", (o["id"],))]
            return self.ok(dict(order=dict(order_no=o["order_no"], product_name=o["product_name"], unit_price=o["unit_price"],
                                           quantity=o["quantity"], total_price=o["total_price"], status=o["status"],
                                           time=fmt_time(o["created_at"]), cards=cards)))

        # ---------- 注册 / 登录 ----------
        if p == "/api/register" and m == "POST":
            b = self.body()
            username, password = str(b.get("username") or "").strip(), str(b.get("password") or "")
            if not re.match(r"^[A-Za-z0-9_一-龥]{2,20}$", username):
                raise ApiError("用户名需为 2-20 位字母/数字/中文/下划线")
            if not (6 <= len(password) <= 64):
                raise ApiError("密码长度需 6-64 位")
            if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise ApiError("用户名已被注册")
            with LOCK:
                con.execute("INSERT INTO users(username,pass_hash,balance,is_admin,status,created_at) VALUES(?,?,0,0,1,?)",
                            (username, hash_pass(password), now()))
                con.commit()
            return self.ok(dict(msg="注册成功，请登录"))
        if p == "/api/login" and m == "POST":
            b = self.body()
            u = con.execute("SELECT * FROM users WHERE username=?", (str(b.get("username") or "").strip(),)).fetchone()
            if not u or u["status"] != 1 or not check_pass(str(b.get("password") or ""), u["pass_hash"]):
                raise ApiError("用户名或密码错误", 401)
            token = secrets.token_hex(32)
            with LOCK:
                con.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token, u["id"], now() + TOKEN_TTL))
                con.commit()
            return self.ok(dict(token=token, user=dict(id=u["id"], username=u["username"], balance=u["balance"], is_admin=bool(u["is_admin"]))))

        # ---------- 以下需登录 ----------
        me = self.need_login(con)
        if p == "/api/me" and m == "GET":
            return self.ok(dict(user=dict(id=me["id"], username=me["username"], balance=me["balance"], is_admin=bool(me["is_admin"]))))
        if p == "/api/logout" and m == "POST":
            token = (self.headers.get("Authorization") or "").split(" ")[-1]
            with LOCK:
                con.execute("DELETE FROM sessions WHERE token=?", (token,)); con.commit()
            return self.ok()
        if p == "/api/change-password" and m == "POST":
            b = self.body()
            u = con.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()
            if not check_pass(str(b.get("old_password") or ""), u["pass_hash"]):
                raise ApiError("原密码不正确")
            npw = str(b.get("new_password") or "")
            if not (6 <= len(npw) <= 64):
                raise ApiError("新密码长度需 6-64 位")
            with LOCK:
                con.execute("UPDATE users SET pass_hash=? WHERE id=?", (hash_pass(npw), me["id"]))
                con.execute("DELETE FROM sessions WHERE user_id=?", (me["id"],)); con.commit()
            return self.ok(dict(msg="密码已修改，请重新登录"))

        # ---------- 下单（原子扣库存 + 扣余额） ----------
        if p == "/api/orders" and m == "POST":
            b = self.body()
            pid, qty = int(b.get("product_id") or 0), int(b.get("quantity") or 0)
            if not pid or not (1 <= qty <= 99):
                raise ApiError("数量需为 1-99")
            with LOCK:
                con.execute("BEGIN IMMEDIATE")
                try:
                    pr = con.execute("SELECT * FROM products WHERE id=? AND status=1", (pid,)).fetchone()
                    if not pr:
                        raise ApiError("商品不存在或已下架", 404)
                    total = money(pr["price"] * qty)
                    bal = con.execute("SELECT balance FROM users WHERE id=?", (me["id"],)).fetchone()["balance"]
                    if bal < total - 1e-9:
                        raise ApiError("余额不足，先去钱包充值")
                    order_no = "XL" + format(int(time.time() * 1000), "X") + secrets.token_hex(2).upper()
                    t = now()
                    if pr["stock_mode"] == "auto":
                        rows = con.execute("SELECT id,code FROM cards WHERE product_id=? AND status='available' ORDER BY id LIMIT ?",
                                           (pid, qty)).fetchall()
                        if len(rows) < qty:
                            raise ApiError("库存不足（仅剩 %d 件）" % len(rows))
                        cur = con.execute("INSERT INTO orders(order_no,user_id,product_id,product_name,unit_price,quantity,total_price,status,created_at) VALUES(?,?,?,?,?,?,?,'done',?)",
                                          (order_no, me["id"], pid, pr["name"], pr["price"], qty, total, t))
                        oid = cur.lastrowid
                        nb = money(bal - total)
                        con.execute("UPDATE users SET balance=? WHERE id=?", (nb, me["id"]))
                        con.execute("INSERT INTO wallet_logs(user_id,amount,balance_after,reason,created_at) VALUES(?,?,?,?,?)",
                                    (me["id"], -total, nb, "购买 %s x%d（%s）" % (pr["name"], qty, order_no), t))
                        for r in rows:
                            con.execute("UPDATE cards SET status='sold',order_id=?,sold_to=?,sold_at=? WHERE id=?", (oid, me["id"], t, r["id"]))
                        op_log(con, me["username"], "发卡", "%s %s x%d" % (order_no, pr["name"], qty))
                        con.commit()
                        return self.ok(dict(order_no=order_no, cards=[r["code"] for r in rows], balance=nb))
                    cur = con.execute("INSERT INTO orders(order_no,user_id,product_id,product_name,unit_price,quantity,total_price,status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
                                      (order_no, me["id"], pid, pr["name"], pr["price"], qty, total, t))
                    nb = money(bal - total)
                    con.execute("UPDATE users SET balance=? WHERE id=?", (nb, me["id"]))
                    con.execute("INSERT INTO wallet_logs(user_id,amount,balance_after,reason,created_at) VALUES(?,?,?,?,?)",
                                (me["id"], -total, nb, "购买 %s x%d（%s，待发货）" % (pr["name"], qty, order_no), t))
                    con.commit()
                    return self.ok(dict(order_no=order_no, cards=[], balance=nb, pending=True))
                except ApiError:
                    con.rollback(); raise
                except Exception as e:
                    con.rollback(); raise ApiError(str(e))
        if p == "/api/orders" and m == "GET":
            out = []
            for o in con.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 100", (me["id"],)):
                cards = [r["code"] for r in con.execute("SELECT code FROM cards WHERE order_id=? ORDER BY id", (o["id"],))]
                out.append(dict(order_no=o["order_no"], product_name=o["product_name"], unit_price=o["unit_price"],
                                quantity=o["quantity"], total_price=o["total_price"], status=o["status"],
                                time=fmt_time(o["created_at"]), cards=cards))
            return self.ok(dict(list=out))

        # ---------- 钱包（模拟充值即时到账） ----------
        if p == "/api/wallet" and m == "GET":
            bal = con.execute("SELECT balance FROM users WHERE id=?", (me["id"],)).fetchone()["balance"]
            logs = [dict(amount=r["amount"], balance_after=r["balance_after"], reason=r["reason"], time=fmt_time(r["created_at"]))
                    for r in con.execute("SELECT * FROM wallet_logs WHERE user_id=? ORDER BY id DESC LIMIT 50", (me["id"],))]
            return self.ok(dict(balance=bal, logs=logs))
        if p == "/api/recharge" and m == "POST":
            b = self.body()
            amount = money(b.get("amount") or 0)
            if not (1 <= amount <= 100000):
                raise ApiError("充值金额需在 1-100000 元之间")
            with LOCK:
                con.execute("BEGIN IMMEDIATE")
                bal = con.execute("SELECT balance FROM users WHERE id=?", (me["id"],)).fetchone()["balance"]
                nb = money(bal + amount)
                con.execute("UPDATE users SET balance=? WHERE id=?", (nb, me["id"]))
                con.execute("INSERT INTO wallet_logs(user_id,amount,balance_after,reason,created_at) VALUES(?,?,?,?,?)",
                            (me["id"], amount, nb, "模拟充值", now()))
                op_log(con, me["username"], "充值", "模拟充值 %.2f" % amount)
                con.commit()
            return self.ok(dict(balance=nb, msg="充值成功（模拟）"))

        # ---------- 管理后台 ----------
        admin = self.need_admin(con)
        if p == "/api/admin/stats" and m == "GET":
            lt = time.localtime()
            day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
            mon0 = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
            g = lambda sql, a=(): con.execute(sql, a).fetchone()["v"]
            top = [dict(name=r["product_name"], qty=r["qty"], amount=r["amount"]) for r in con.execute(
                "SELECT product_name, SUM(quantity) qty, SUM(total_price) amount FROM orders WHERE status='done' GROUP BY product_name ORDER BY qty DESC LIMIT 5")]
            low = [dict(id=r["id"], name=r["name"], stock=stock_of(con, r["id"])) for r in con.execute(
                "SELECT id,name FROM products WHERE status=1 AND stock_mode='auto'")]
            low = [x for x in low if x["stock"] < 5][:10]
            return self.ok(dict(
                users=g("SELECT COUNT(*) v FROM users"), products=g("SELECT COUNT(*) v FROM products WHERE status=1"),
                orders=g("SELECT COUNT(*) v FROM orders"), pending_orders=g("SELECT COUNT(*) v FROM orders WHERE status='pending'"),
                today_revenue=g("SELECT COALESCE(SUM(total_price),0) v FROM orders WHERE status='done' AND created_at>=?", (day0,)),
                month_revenue=g("SELECT COALESCE(SUM(total_price),0) v FROM orders WHERE status='done' AND created_at>=?", (mon0,)),
                total_revenue=g("SELECT COALESCE(SUM(total_price),0) v FROM orders WHERE status='done'"),
                top_products=top, low_stock=low))
        if p == "/api/admin/users" and m == "GET":
            qq = q.get("q", [""])[0].strip()
            sql = "SELECT id,username,balance,is_admin,status,created_at FROM users ORDER BY id DESC LIMIT 200"
            args = []
            if qq:
                sql = "SELECT id,username,balance,is_admin,status,created_at FROM users WHERE username LIKE ? ORDER BY id DESC LIMIT 200"; args = ["%" + qq + "%"]
            return self.ok(dict(list=[dict(id=r["id"], username=r["username"], balance=r["balance"], is_admin=bool(r["is_admin"]),
                                            status=r["status"], time=fmt_time(r["created_at"])) for r in con.execute(sql, args)]))
        ub = re.match(r"^/api/admin/users/(\d+)/balance$", p)
        if ub and m == "POST":
            b = self.body(); amount = money(b.get("amount") or 0)
            if amount == 0 or abs(amount) > 1000000:
                raise ApiError("调整金额无效")
            with LOCK:
                con.execute("BEGIN IMMEDIATE")
                u = con.execute("SELECT balance FROM users WHERE id=?", (int(ub.group(1)),)).fetchone()
                if not u:
                    con.rollback(); raise ApiError("用户不存在")
                nb = money(u["balance"] + amount)
                if nb < 0:
                    con.rollback(); raise ApiError("调整后余额不能为负")
                con.execute("UPDATE users SET balance=? WHERE id=?", (nb, int(ub.group(1))))
                con.execute("INSERT INTO wallet_logs(user_id,amount,balance_after,reason,created_at) VALUES(?,?,?,?,?)",
                            (int(ub.group(1)), amount, nb, "管理员调整：" + str(b.get("remark") or "")[:200], now()))
                op_log(con, admin["username"], "调整余额", "user %s %+0.2f" % (ub.group(1), amount))
                con.commit()
            return self.ok(dict(balance=nb))
        us = re.match(r"^/api/admin/users/(\d+)/status$", p)
        if us and m == "POST":
            b = self.body()
            with LOCK:
                con.execute("UPDATE users SET status=? WHERE id=?", (1 if b.get("status") else 0, int(us.group(1)))); con.commit()
            return self.ok()
        if p == "/api/admin/categories" and m == "GET":
            return self.ok(dict(list=[dict(r) for r in con.execute("SELECT * FROM categories ORDER BY sort,id")]))
        if p == "/api/admin/categories" and m == "POST":
            b = self.body(); name = str(b.get("name") or "").strip()
            if not name:
                raise ApiError("分类名不能为空")
            with LOCK:
                con.execute("INSERT INTO categories(name,sort) VALUES(?,?)", (name[:30], int(b.get("sort") or 0))); con.commit()
            return self.ok()
        cd = re.match(r"^/api/admin/categories/(\d+)$", p)
        if cd and m == "DELETE":
            with LOCK:
                con.execute("DELETE FROM categories WHERE id=?", (int(cd.group(1)),)); con.commit()
            return self.ok()
        if p == "/api/admin/products" and m == "GET":
            out = []
            for r in con.execute("""SELECT p.*, c.name category_name FROM products p
                                    LEFT JOIN categories c ON c.id=p.category_id ORDER BY p.sort,p.id"""):
                out.append(dict(id=r["id"], category_id=r["category_id"], category_name=r["category_name"], name=r["name"],
                                description=r["description"], cover=r["cover"], price=r["price"], stock_mode=r["stock_mode"],
                                status=r["status"], sort=r["sort"], stock=stock_of(con, r["id"]),
                                sold=con.execute("SELECT COUNT(*) c FROM cards WHERE product_id=? AND status='sold'", (r["id"],)).fetchone()["c"]))
            return self.ok(dict(list=out))
        if p == "/api/admin/products" and m == "POST":
            b = self.body(); name = str(b.get("name") or "").strip(); price = money(b.get("price") or 0)
            if not name:
                raise ApiError("商品名不能为空")
            if price <= 0:
                raise ApiError("价格无效")
            with LOCK:
                con.execute("INSERT INTO products(category_id,name,description,cover,price,stock_mode,status,sort,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (int(b["category_id"]) if b.get("category_id") else None, name[:100], str(b.get("description") or "")[:2000],
                             str(b.get("cover") or "")[:500], price, "manual" if b.get("stock_mode") == "manual" else "auto",
                             0 if b.get("status") == 0 else 1, int(b.get("sort") or 0), now())); con.commit()
            return self.ok()
        pdu = re.match(r"^/api/admin/products/(\d+)$", p)
        if pdu and m == "PUT":
            b = self.body(); pid = int(pdu.group(1))
            old = con.execute("SELECT price FROM products WHERE id=?", (pid,)).fetchone()
            if not old:
                raise ApiError("商品不存在", 404)
            with LOCK:
                con.execute("UPDATE products SET category_id=?,name=?,description=?,cover=?,price=?,stock_mode=?,status=?,sort=? WHERE id=?",
                            (int(b["category_id"]) if b.get("category_id") else None, str(b.get("name") or "").strip()[:100],
                             str(b.get("description") or "")[:2000], str(b.get("cover") or "")[:500], money(b.get("price") or 0),
                             "manual" if b.get("stock_mode") == "manual" else "auto", 0 if b.get("status") == 0 else 1,
                             int(b.get("sort") or 0), pid))
                if money(b.get("price") or 0) != old["price"]:
                    op_log(con, admin["username"], "改价", "product %d %.2f -> %.2f" % (pid, old["price"], money(b.get("price") or 0)))
                con.commit()
            return self.ok()
        if pdu and m == "DELETE":
            pid = int(pdu.group(1))
            with LOCK:
                r = con.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
                con.execute("DELETE FROM products WHERE id=?", (pid,))
                if r:
                    op_log(con, admin["username"], "删商品", "%s（id %d）" % (r["name"], pid))
                con.commit()
            return self.ok()
        pci = re.match(r"^/api/admin/products/(\d+)/cards$", p)
        if pci and m == "POST":
            b = self.body()
            codes = [s.strip() for s in re.split(r"[\r\n,;，；]+", str(b.get("codes") or "")) if s.strip()]
            if not codes:
                raise ApiError("请粘贴卡密，每行一条")
            with LOCK:
                con.executemany("INSERT INTO cards(product_id,code,status,created_at) VALUES(?,?,'available',?)",
                                [(int(pci.group(1)), c[:500], now()) for c in codes[:5000]])
                con.commit()
            return self.ok(dict(added=len(codes[:5000])))
        if pci and m == "GET":
            st = q.get("status", [""])[0]
            sql = "SELECT id,code,status,sold_at FROM cards WHERE product_id=?"; args = [int(pci.group(1))]
            if st in ("available", "sold"):
                sql += " AND status=?"; args.append(st)
            sql += " ORDER BY id DESC LIMIT 500"
            return self.ok(dict(list=[dict(id=r["id"], code=r["code"], status=r["status"],
                                            time=fmt_time(r["sold_at"]) if r["sold_at"] else "") for r in con.execute(sql, args)]))
        cdel = re.match(r"^/api/admin/cards/(\d+)$", p)
        if cdel and m == "DELETE":
            with LOCK:
                con.execute("DELETE FROM cards WHERE id=? AND status='available'", (int(cdel.group(1)),)); con.commit()
            return self.ok()
        if p == "/api/admin/orders" and m == "GET":
            st = q.get("status", [""])[0]
            sql = "SELECT o.*, u.username FROM orders o JOIN users u ON u.id=o.user_id"; args = []
            if st in ("pending", "done", "cancelled"):
                sql += " WHERE o.status=?"; args.append(st)
            sql += " ORDER BY o.id DESC LIMIT 200"
            out = []
            for o in con.execute(sql, args):
                cards = [r["code"] for r in con.execute("SELECT code FROM cards WHERE order_id=? ORDER BY id", (o["id"],))]
                out.append(dict(id=o["id"], order_no=o["order_no"], username=o["username"], product_name=o["product_name"],
                                quantity=o["quantity"], total_price=o["total_price"], status=o["status"],
                                time=fmt_time(o["created_at"]), cards=cards))
            return self.ok(dict(list=out))
        ful = re.match(r"^/api/admin/orders/(\d+)/fulfill$", p)
        if ful and m == "POST":
            b = self.body(); oid = int(ful.group(1))
            with LOCK:
                con.execute("BEGIN IMMEDIATE")
                try:
                    o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                    if not o or o["status"] != "pending":
                        raise ApiError("订单不存在或无需发货")
                    codes = [s.strip() for s in re.split(r"[\r\n,;，；]+", str(b.get("codes") or "")) if s.strip()]
                    t = now()
                    if not codes:
                        rows = con.execute("SELECT id,code FROM cards WHERE product_id=? AND status='available' ORDER BY id LIMIT ?",
                                           (o["product_id"], o["quantity"])).fetchall()
                        if len(rows) < o["quantity"]:
                            raise ApiError("库存不足（仅剩 %d 件）" % len(rows))
                        for r in rows:
                            con.execute("UPDATE cards SET status='sold',order_id=?,sold_to=?,sold_at=? WHERE id=?", (oid, o["user_id"], t, r["id"]))
                        codes = [r["code"] for r in rows]
                    else:
                        if len(codes) < o["quantity"]:
                            raise ApiError("卡密数量不足（需要 %d 条）" % o["quantity"])
                        codes = codes[: o["quantity"]]
                        con.executemany("INSERT INTO cards(product_id,code,status,order_id,sold_to,sold_at,created_at) VALUES(?,?,'sold',?,?,?,?)",
                                        [(o["product_id"], c[:500], oid, o["user_id"], t, t) for c in codes])
                    con.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
                    op_log(con, admin["username"], "人工发货", o["order_no"])
                    con.commit()
                    return self.ok(dict(cards=codes))
                except ApiError:
                    con.rollback(); raise
                except Exception as e:
                    con.rollback(); raise ApiError(str(e))
        ocx = re.match(r"^/api/admin/orders/(\d+)/cancel$", p)
        if ocx and m == "POST":
            oid = int(ocx.group(1))
            with LOCK:
                con.execute("BEGIN IMMEDIATE")
                try:
                    o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                    if not o or o["status"] != "pending":
                        raise ApiError("只有待发货订单可以取消")
                    con.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
                    bal = con.execute("SELECT balance FROM users WHERE id=?", (o["user_id"],)).fetchone()["balance"]
                    nb = money(bal + o["total_price"])
                    con.execute("UPDATE users SET balance=? WHERE id=?", (nb, o["user_id"]))
                    con.execute("INSERT INTO wallet_logs(user_id,amount,balance_after,reason,created_at) VALUES(?,?,?,?,?)",
                                (o["user_id"], o["total_price"], nb, "订单取消退款（%s）" % o["order_no"], now()))
                    con.commit()
                    return self.ok()
                except ApiError:
                    con.rollback(); raise
                except Exception as e:
                    con.rollback(); raise ApiError(str(e))
        if p == "/api/admin/announcements" and m == "POST":
            b = self.body(); content = str(b.get("content") or "").strip()
            if not content:
                raise ApiError("内容不能为空")
            with LOCK:
                con.execute("INSERT INTO announcements(content,created_at) VALUES(?,?)", (content[:1000], now())); con.commit()
            return self.ok()
        ad = re.match(r"^/api/admin/announcements/(\d+)$", p)
        if ad and m == "DELETE":
            with LOCK:
                con.execute("DELETE FROM announcements WHERE id=?", (int(ad.group(1)),)); con.commit()
            return self.ok()
        if p == "/api/admin/logs" and m == "GET":
            return self.ok(dict(list=[dict(actor=r["actor"], action=r["action"], detail=r["detail"], time=fmt_time(r["created_at"]))
                                       for r in con.execute("SELECT * FROM op_logs ORDER BY id DESC LIMIT 100")]))
        raise ApiError("接口不存在", 404)


if __name__ == "__main__":
    init_db()
    print("[小黎卡网] 已启动 http://127.0.0.1:%d  数据库 %s" % (PORT, DB_PATH))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
