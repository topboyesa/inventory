"""Modern Inventory Management - tkinter + SQLite.   v0.3

Run:            python inventory_app.py
Forgot admin?   python inventory_app.py --recover
Optional libs:  pip install pillow                                   (profile pictures)
                pip install google-api-python-client google-auth-oauthlib   (Google Drive backups)

Your data lives OUTSIDE the app folder, in  ~/ModernInventory/  (override with the
INVENTORY_DATA_DIR environment variable), so deleting/reinstalling the app keeps it.
The first account created is the admin; after that only admins add employees.
"""
import argparse
import getpass
import hashlib
import os
import re
import secrets
import shutil
import smtplib
import socket
import sqlite3
import sys
import threading
import time
import traceback
import tkinter as tk
from datetime import datetime
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = None

APP_VERSION = "0.3.0"
DATA_DIR = Path(os.getenv("INVENTORY_DATA_DIR") or Path.home() / "ModernInventory")
DB = DATA_DIR / "inventory.db"
BACKUP_DIR = DATA_DIR / "backups"
CREDENTIALS = DATA_DIR / "credentials.json"      # OAuth client file from Google Cloud Console
TOKEN = DATA_DIR / "drive_token.json"            # created after you press "Connect Google Drive"
DRIVE_FOLDER = "Modern Inventory Back-up"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]   # app can only see files it created
KEEP_BACKUPS = 30
MIN_PW = 8
COLORS = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
          "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]
LOGIN_FAILS = {}   # email -> (failed_count, locked_until)  - a speed bump, not a vault


# ---------------------------------------------------------------- database
def db(sql, params=()):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def add_column(con, table, col, decl):
    if col not in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy = Path("inventory.db")                  # database from v0.1/v0.2 next to the script
    if not DB.exists() and legacy.exists():
        shutil.copy(legacy, DB)
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE,
        salt TEXT, pw_hash TEXT, role TEXT DEFAULT 'employee');
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY, name TEXT UNIQUE, quantity INTEGER, price REAL);
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY, product_id INTEGER, product_name TEXT,
        qty INTEGER, status TEXT DEFAULT 'pending', created TEXT);
    CREATE TABLE IF NOT EXISTS sales(
        id INTEGER PRIMARY KEY, product_name TEXT, qty INTEGER,
        total REAL, sold_at TEXT, buyer_name TEXT, buyer_phone TEXT);
    CREATE TABLE IF NOT EXISTS announcements(
        id INTEGER PRIMARY KEY, author TEXT, message TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS reactions(
        announcement_id INTEGER, user_id INTEGER, value INTEGER,
        synced INTEGER DEFAULT 0, PRIMARY KEY(announcement_id, user_id));
    CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY, announcement_id INTEGER, user_id INTEGER,
        author TEXT, text TEXT, created TEXT, synced INTEGER DEFAULT 0);
    """)
    add_column(con, "sales", "buyer_name", "TEXT")
    add_column(con, "sales", "buyer_phone", "TEXT")
    add_column(con, "users", "avatar", "BLOB")
    if not con.execute("SELECT 1 FROM products").fetchone():
        con.executemany("INSERT INTO products(name,quantity,price) VALUES(?,?,?)",
                        [(f"test_product{i}", 20 * i, 100.0 * i) for i in range(1, 5)])
    con.commit()
    con.close()


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def set_password(user_id, pw):
    salt = secrets.token_hex(16)
    db("UPDATE users SET salt=?, pw_hash=? WHERE id=?", (salt, hash_pw(pw, salt), user_id))


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------- lost-password recovery
def recover():
    """Terminal tool: reset any account's password (needs access to this computer)."""
    users = db("SELECT id,name,email,role FROM users ORDER BY id")
    if not users:
        return print("No accounts yet - just start the app and create the admin account.")
    for u in users:
        print(f"  {u[0]}. {u[1]} <{u[2]}> [{u[3]}]")
    try:
        uid = int(input("Account number to recover: "))
    except ValueError:
        return print("Cancelled.")
    if uid not in [u[0] for u in users]:
        return print("No such account.")
    pw = getpass.getpass(f"New password ({MIN_PW}+ characters): ")
    if len(pw) < MIN_PW or pw != getpass.getpass("Repeat password: "):
        return print("Passwords must match and be long enough. Nothing changed.")
    set_password(uid, pw)
    new_email = input("New email for this account (Enter to keep): ").strip().lower()
    if new_email:
        try:
            db("UPDATE users SET email=? WHERE id=?", (new_email, uid))
        except sqlite3.IntegrityError:
            print("That email is already used; email left unchanged.")
    if input("Make this account an admin? (y/N): ").lower() == "y":
        db("UPDATE users SET role='admin' WHERE id=?", (uid,))
    print("Done. You can log in now.")


# --------------------------------------------------------------- backups
def online():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2).close()
        return True
    except OSError:
        return False


def drive_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:                                       # first time: opens your browser
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
        try:
            os.chmod(TOKEN, 0o600)
        except OSError:
            pass
    return build("drive", "v3", credentials=creds)


def drive_folder(svc):
    q = f"name='{DRIVE_FOLDER}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    found = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    if found:
        return found[0]["id"]
    body = {"name": DRIVE_FOLDER, "mimeType": "application/vnd.google-apps.folder"}
    return svc.files().create(body=body, fields="id").execute()["id"]


def upload_to_cloud(path):
    if not TOKEN.exists():                          # not connected yet -> keep local only
        return False
    try:
        from googleapiclient.http import MediaFileUpload
        svc = drive_service()
        folder = drive_folder(svc)
        media = MediaFileUpload(str(path), mimetype="application/octet-stream")
        q = f"name='{path.name}' and '{folder}' in parents and trashed=false"
        found = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
        if found:
            svc.files().update(fileId=found[0]["id"], media_body=media).execute()
        else:
            svc.files().create(body={"name": path.name, "parents": [folder]},
                               media_body=media).execute()
        return True
    except Exception as e:
        print("Drive upload failed:", e)
        return False


def backup_db(upload=True):
    """Refresh today's backup copy, then push any not-yet-uploaded backups when online."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"inventory_{datetime.now():%Y%m%d}.db"
    src, dst = sqlite3.connect(DB), sqlite3.connect(dest)
    src.backup(dst)
    src.close()
    dst.close()
    Path(str(dest) + ".uploaded").unlink(missing_ok=True)      # changed -> needs re-upload
    for old in sorted(BACKUP_DIR.glob("inventory_*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
        Path(str(old) + ".uploaded").unlink(missing_ok=True)
    if upload and TOKEN.exists() and online():
        for p in sorted(BACKUP_DIR.glob("inventory_*.db")):
            marker = Path(str(p) + ".uploaded")
            if not marker.exists() and upload_to_cloud(p):
                marker.touch()


def restore_from(path):
    """Replace the live database with a backup file. Returns an error string or None."""
    ok, tables = False, set()
    try:
        con = sqlite3.connect(path)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return "That file is not a valid database."
    if not ok or not {"users", "products", "sales"} <= tables:
        return "That doesn't look like an Inventory backup."
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if DB.exists():                                 # safety copy of what we're replacing
        shutil.copy(DB, BACKUP_DIR / f"before_restore_{datetime.now():%Y%m%d_%H%M%S}.db")
    src, dst = sqlite3.connect(path), sqlite3.connect(DB)
    src.backup(dst)
    src.close()
    dst.close()
    init_db()                                       # upgrade old backups to the current schema
    return None


def pending_sync():
    """Comments/reactions waiting to be sent to a central server (none exists yet)."""
    return sum(db(f"SELECT COUNT(*) FROM {t} WHERE synced=0")[0][0] for t in ("comments", "reactions"))


def email_employees(subject, body):
    host, user, pw = (os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
    if not (host and user and pw):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, user
    msg["To"] = ", ".join(r[0] for r in db("SELECT email FROM users"))
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


# ---------------------------------------------------------------- avatars
def make_avatar_png(path, size=140):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    img = img.crop((left, top, left + s, top + s)).resize((size, size))
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def circular_photo(png_bytes, size=70):
    img = Image.open(BytesIO(png_bytes)).convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    img.putalpha(mask)
    return ImageTk.PhotoImage(img)


# --------------------------------------------------------------------- UI
def make_table(parent, cols, widths, height=14):
    tree = ttk.Treeview(parent, columns=cols, show="headings", height=height)
    for c, w in zip(cols, widths):
        tree.heading(c, text=c.title())
        tree.column(c, width=w, anchor="w")
    tree.pack(fill="both", expand=True, pady=8)
    return tree


def selected(tree):
    sel = tree.selection()
    return tree.item(sel[0])["values"] if sel else None


class BuyerDialog(simpledialog.Dialog):
    """Asks for buyer name + phone; .result is (name, phone) or None if cancelled."""

    def __init__(self, parent, summary):
        self.summary = summary
        super().__init__(parent, "Complete sale")

    def body(self, master):
        ttk.Label(master, text=self.summary, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 8))
        ttk.Label(master, text="Buyer name").grid(row=1, column=0, sticky="w", pady=3)
        self.name = ttk.Entry(master, width=30)
        self.name.grid(row=1, column=1, pady=3)
        ttk.Label(master, text="Phone number").grid(row=2, column=0, sticky="w", pady=3)
        self.phone = ttk.Entry(master, width=30)
        self.phone.grid(row=2, column=1, pady=3)
        return self.name

    def validate(self):
        name, phone = self.name.get().strip(), self.phone.get().strip()
        digits = re.sub(r"\D", "", phone)
        if not name:
            messagebox.showwarning("Missing", "Enter the buyer's name.", parent=self)
            return False
        if not re.fullmatch(r"\+?[\d\s\-()]+", phone) or not 9 <= len(digits) <= 15:
            messagebox.showwarning("Invalid phone", "Enter a valid phone number, e.g. 0712 345 678 or +254712345678.", parent=self)
            return False
        self._vals = (name, phone)
        return True

    def apply(self):
        self.result = self._vals


class LoginPage(ttk.Frame):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        first_run = not db("SELECT 1 FROM users")
        box = ttk.Frame(self)
        box.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(box, text="Modern Inventory Management", font=("Segoe UI", 18, "bold")).pack(pady=10)
        ttk.Label(box, text="First run: create the admin account" if first_run else "Login").pack()
        ttk.Label(box, text="Email").pack(anchor="w")
        self.email = ttk.Entry(box, width=32)
        self.email.pack(pady=2)
        ttk.Label(box, text="Password").pack(anchor="w")
        self.pw = ttk.Entry(box, width=32, show="*")
        self.pw.pack(pady=2)
        self.pw.bind("<Return>", lambda e: self.register() if first_run else self.login())
        if first_run:
            ttk.Button(box, text="Create admin account", command=self.register).pack(fill="x", pady=(10, 2))
            ttk.Button(box, text="Restore from backup file...", command=self.restore).pack(fill="x", pady=2)
        else:
            ttk.Button(box, text="Login", command=self.login).pack(fill="x", pady=(10, 2))
            ttk.Button(box, text="Login with Google", command=lambda: messagebox.showinfo(
                "Coming soon", "Google sign-in needs an OAuth client; see notes.")).pack(fill="x", pady=2)
            ttk.Label(box, text="Forgot your password? Run ModernInventory-Recover.exe\n(or: python inventory_app.py --recover)",
                      foreground="gray").pack(pady=(10, 0))
        ttk.Label(box, text=f"v{APP_VERSION}", foreground="gray").pack(pady=(8, 0))

    def login(self):
        email = self.email.get().strip().lower()
        fails, until = LOGIN_FAILS.get(email, (0, 0))
        if time.time() < until:
            return messagebox.showerror("Locked", f"Too many attempts. Try again in {int(until - time.time()) // 60 + 1} min.")
        row = db("SELECT id,name,email,salt,pw_hash,role FROM users WHERE email=?", (email,))
        if row and secrets.compare_digest(row[0][4], hash_pw(self.pw.get(), row[0][3])):
            LOGIN_FAILS.pop(email, None)
            self.app.user = {"id": row[0][0], "name": row[0][1], "email": row[0][2], "role": row[0][5]}
            self.app.swap(MainPage)
        else:
            fails += 1
            LOGIN_FAILS[email] = (0, time.time() + 300) if fails >= 5 else (fails, 0)
            messagebox.showerror("Login failed", "Wrong email or password.")

    def register(self):
        email, pw = self.email.get().strip().lower(), self.pw.get()
        if "@" not in email or len(pw) < MIN_PW:
            return messagebox.showwarning("Check details", f"Use a real email (for recovery) and a {MIN_PW}+ character password.")
        name = simpledialog.askstring("Create admin", "Your name:")
        if not name:
            return
        salt = secrets.token_hex(16)
        db("INSERT INTO users(name,email,salt,pw_hash,role) VALUES(?,?,?,?,'admin')",
           (name, email, salt, hash_pw(pw, salt)))
        messagebox.showinfo("Done", "Admin account created. You can log in now.")
        self.app.swap(LoginPage)

    def restore(self):
        path = filedialog.askopenfilename(title="Choose a backup", filetypes=[("Database", "*.db"), ("All", "*.*")])
        if path:
            err = restore_from(path)
            messagebox.showerror("Restore failed", err) if err else messagebox.showinfo("Restored", "Data restored. Log in with your old account.")
            self.app.swap(LoginPage)


class MainPage(ttk.Frame):
    def __init__(self, app):
        super().__init__(app)
        self.app, self.user = app, app.user
        self.is_admin = self.user["role"] == "admin"

        side = ttk.Frame(self, padding=10)
        side.pack(side="left", fill="y")
        self.av = tk.Canvas(side, width=70, height=70, highlightthickness=0)
        self.av.pack()
        ttk.Button(side, text="Change photo", command=self.change_photo).pack(pady=(2, 0))
        ttk.Label(side, text=f"Hello {self.user['name']}").pack(pady=(4, 0))
        ttk.Label(side, text=self.user["role"].title(), foreground="gray").pack(pady=(0, 12))
        nav = [("Inventory", self.show_inventory), ("Orders", self.show_orders),
               ("Sales", self.show_sales), ("Employees", self.show_employees),
               ("Announcements", self.show_announcements)]
        if self.is_admin:
            nav.append(("Backup", self.show_backup))
        for text, fn in nav + [("Logout", self.logout)]:
            ttk.Button(side, text=text, command=fn, width=18).pack(pady=3)
        ttk.Label(side, text=f"v{APP_VERSION}", foreground="gray").pack(side="bottom")

        self.content = ttk.Frame(self, padding=15)
        self.content.pack(side="left", fill="both", expand=True)
        self.draw_avatar()
        self.show_inventory()

    # ---- shared helpers
    def clear(self, title):
        for w in self.content.winfo_children():
            w.destroy()
        ttk.Label(self.content, text=title, font=("Segoe UI", 16, "bold")).pack(anchor="w")

    def buttons(self, items):
        """items: (text, command) or (text, command, enabled)."""
        bar = ttk.Frame(self.content)
        bar.pack(anchor="w")
        for it in items:
            b = ttk.Button(bar, text=it[0], command=it[1])
            if len(it) > 2 and not it[2]:
                b.state(["disabled"])
            b.pack(side="left", padx=(0, 6))

    def require_admin(self):
        if not self.is_admin:
            messagebox.showwarning("Admins only", "Only an admin can do that.")
        return self.is_admin

    # ---- profile picture
    def draw_avatar(self):
        self.av.delete("all")
        blob = db("SELECT avatar FROM users WHERE id=?", (self.user["id"],))[0][0]
        if blob and Image:
            try:
                self.photo = circular_photo(blob)       # keep a reference or Tk drops it
                self.av.create_image(35, 35, image=self.photo)
                return
            except Exception:
                pass
        self.av.create_oval(2, 2, 68, 68, fill="#4e79a7", outline="")
        self.av.create_text(35, 35, text=self.user["name"][:1].upper(), fill="white", font=("Segoe UI", 24, "bold"))

    def change_photo(self):
        if Image is None:
            return messagebox.showinfo("Pillow needed", "Install it with:\n\npip install pillow\n\nthen restart the app.")
        path = filedialog.askopenfilename(title="Choose a photo",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp")])
        if not path:
            return
        try:
            png = make_avatar_png(path)
        except Exception:
            return messagebox.showerror("Image", "Couldn't read that image.")
        db("UPDATE users SET avatar=? WHERE id=?", (png, self.user["id"]))
        self.draw_avatar()

    # ---- inventory
    def show_inventory(self):
        self.clear("Inventory")
        self.inv = make_table(self.content, ("id", "name", "quantity", "price"), (50, 250, 100, 100))
        self.inv.tag_configure("low", foreground="red")
        for r in db("SELECT id,name,quantity,price FROM products ORDER BY name"):
            self.inv.insert("", "end", values=r, tags=("low",) if r[2] <= 5 else ())
        a = self.is_admin
        self.buttons([("Add product", self.add_product, a), ("Set quantity", self.set_qty, a),
                      ("Delete product", self.del_product, a), ("Create order", self.create_order)])
        if not a:
            ttk.Label(self.content, text="Add / quantity / delete are admin-only. You are logged in as an employee.",
                      foreground="gray").pack(anchor="w", pady=6)

    def add_product(self):
        if not self.require_admin():
            return
        name = simpledialog.askstring("Add product", "Name:")
        qty = simpledialog.askinteger("Add product", "Quantity:", minvalue=0) if name else None
        price = simpledialog.askfloat("Add product", "Price:", minvalue=0) if qty is not None else None
        if price is not None:
            try:
                db("INSERT INTO products(name,quantity,price) VALUES(?,?,?)", (name.strip(), qty, price))
            except sqlite3.IntegrityError:
                messagebox.showerror("Exists", "Product name already exists.")
            self.show_inventory()

    def set_qty(self):
        if not self.require_admin():
            return
        row = selected(self.inv)
        if not row:
            return messagebox.showinfo("Select", "Click a product first.")
        q = simpledialog.askinteger("Quantity", f"New quantity for {row[1]}:", minvalue=0)
        if q is not None:
            db("UPDATE products SET quantity=? WHERE id=?", (q, row[0]))
            self.show_inventory()

    def del_product(self):
        if not self.require_admin():
            return
        row = selected(self.inv)
        if not row:
            return messagebox.showinfo("Select", "Click a product first.")
        if db("SELECT COUNT(*) FROM orders WHERE product_id=? AND status='pending'", (row[0],))[0][0]:
            return messagebox.showwarning("In use", "This product has pending orders. Complete or cancel them first.")
        if messagebox.askyesno("Delete", f"Delete {row[1]}?"):
            db("DELETE FROM products WHERE id=?", (row[0],))
            self.show_inventory()

    def create_order(self):
        row = selected(self.inv)
        if not row:
            return messagebox.showinfo("Select", "Click a product first.")
        q = simpledialog.askinteger("Order", f"Quantity of {row[1]}:", minvalue=1)
        if q:
            db("INSERT INTO orders(product_id,product_name,qty,created) VALUES(?,?,?,?)",
               (row[0], row[1], q, now()))
            messagebox.showinfo("Order", "Order added. Open the Orders page to complete the sale.")

    # ---- orders
    def show_orders(self):
        self.clear("Pending orders")
        self.ord = make_table(self.content, ("id", "product", "qty", "created"), (50, 250, 80, 150))
        for r in db("SELECT id,product_name,qty,created FROM orders WHERE status='pending' ORDER BY id"):
            self.ord.insert("", "end", values=r)
        self.buttons([("Complete sale", self.complete_sale), ("Cancel order", self.cancel_order)])
        ttk.Label(self.content, text="Click an order, then press Complete sale.", foreground="gray").pack(anchor="w")

    def complete_sale(self):
        row = selected(self.ord)
        if not row:
            return messagebox.showinfo("Select an order", "Click an order in the list first, then press Complete sale.")
        o = db("SELECT product_id,product_name,qty FROM orders WHERE id=? AND status='pending'", (row[0],))
        if not o:
            return self.show_orders()
        pid, name, qty = o[0]
        p = db("SELECT quantity,price FROM products WHERE id=?", (pid,))
        if not p:
            return messagebox.showerror("Missing", "That product no longer exists.")
        if p[0][0] < qty:
            return messagebox.showerror("Stock", f"Only {p[0][0]} in stock.")
        total = qty * p[0][1]
        dlg = BuyerDialog(self, f"{qty} x {name} = {total:,.2f}")
        if not dlg.result:                              # cancelled: order stays pending
            return
        buyer_name, buyer_phone = dlg.result
        db("UPDATE products SET quantity=quantity-? WHERE id=?", (qty, pid))
        db("INSERT INTO sales(product_name,qty,total,sold_at,buyer_name,buyer_phone) VALUES(?,?,?,?,?,?)",
           (name, qty, total, now(), buyer_name, buyer_phone))
        db("UPDATE orders SET status='completed' WHERE id=?", (row[0],))
        self.show_orders()

    def cancel_order(self):
        row = selected(self.ord)
        if not row:
            return messagebox.showinfo("Select an order", "Click an order first.")
        db("UPDATE orders SET status='cancelled' WHERE id=?", (row[0],))
        self.show_orders()

    # ---- sales
    def show_sales(self):
        self.clear("Sales")
        units, revenue = db("SELECT COALESCE(SUM(qty),0), COALESCE(SUM(total),0) FROM sales")[0]
        ttk.Label(self.content, text=f"Total units sold: {units}     Revenue: {revenue:,.2f}",
                  font=("Segoe UI", 12)).pack(anchor="w", pady=8)
        self.buttons([("Sales history", self.show_history)])
        data = db("SELECT product_name, SUM(qty) FROM sales GROUP BY product_name ORDER BY 2 DESC")
        cv = tk.Canvas(self.content, width=560, height=320, highlightthickness=0)
        cv.pack(anchor="w")
        if not data:
            return cv.create_text(280, 160, text="No sales yet")
        start = 90
        for i, (name, qty) in enumerate(data):
            extent = -min(360 * qty / units, 359.99)
            c = COLORS[i % len(COLORS)]
            cv.create_arc(10, 10, 310, 310, start=start, extent=extent, fill=c, outline="white")
            start += extent
            cv.create_rectangle(340, 20 + i * 24, 354, 34 + i * 24, fill=c, outline="")
            cv.create_text(362, 27 + i * 24, anchor="w", text=f"{name} ({qty}, {qty / units:.0%})")

    def show_history(self):
        self.clear("Sales history")
        t = make_table(self.content, ("date", "product", "qty", "total", "buyer", "phone"),
                       (130, 160, 50, 90, 150, 120))
        for r in db("SELECT sold_at,product_name,qty,total,COALESCE(buyer_name,'-'),"
                    "COALESCE(buyer_phone,'-') FROM sales ORDER BY id DESC"):
            t.insert("", "end", values=r)
        self.buttons([("Back to sales", self.show_sales)])

    # ---- employees
    def show_employees(self):
        self.clear("Employees")
        self.emp = make_table(self.content, ("id", "name", "email", "role"), (50, 180, 250, 100))
        for r in db("SELECT id,name,email,role FROM users ORDER BY name"):
            self.emp.insert("", "end", values=r)
        if self.is_admin:
            self.buttons([("Add employee", self.add_employee), ("Reset password", self.reset_password),
                          ("Toggle admin", self.toggle_role)])

    def add_employee(self):
        if not self.require_admin():
            return
        name = simpledialog.askstring("Add employee", "Name:")
        email = simpledialog.askstring("Add employee", "Email:") if name else None
        pw = simpledialog.askstring("Add employee", f"Temporary password ({MIN_PW}+ chars):", show="*") if email else None
        if not pw:
            return
        if "@" not in email or len(pw) < MIN_PW:
            return messagebox.showwarning("Check details", "Valid email and a longer password, please.")
        salt = secrets.token_hex(16)
        try:
            db("INSERT INTO users(name,email,salt,pw_hash,role) VALUES(?,?,?,?,'employee')",
               (name.strip(), email.strip().lower(), salt, hash_pw(pw, salt)))
        except sqlite3.IntegrityError:
            messagebox.showerror("Exists", "That email is already registered.")
        self.show_employees()

    def reset_password(self):
        if not self.require_admin():
            return
        row = selected(self.emp)
        if not row:
            return messagebox.showinfo("Select", "Click an employee first.")
        pw = simpledialog.askstring("Reset password", f"New password for {row[1]}:", show="*")
        if pw and len(pw) >= MIN_PW:
            set_password(row[0], pw)
            messagebox.showinfo("Done", "Password changed.")
        elif pw:
            messagebox.showwarning("Too short", f"Use at least {MIN_PW} characters.")

    def toggle_role(self):
        if not self.require_admin():
            return
        row = selected(self.emp)
        if row and row[0] != self.user["id"]:
            db("UPDATE users SET role=? WHERE id=?", ("employee" if row[3] == "admin" else "admin", row[0]))
            self.show_employees()

    # ---- announcements (+ like / dislike / comments)
    def show_announcements(self, select_id=None):
        self.clear("Announcements")
        if self.is_admin:
            self.buttons([("+ New announcement", self.new_announcement)])
        self.ann = make_table(self.content, ("id", "date", "author", "message"), (40, 130, 120, 400), height=6)
        rows = db("SELECT id,created,author,message FROM announcements ORDER BY id DESC")
        for r in rows:
            self.ann.insert("", "end", iid=str(r[0]), values=(r[0], r[1], r[2], r[3].replace("\n", " ")[:70]))
        self.ann_body = tk.Text(self.content, height=4, wrap="word", state="disabled")
        self.ann_body.pack(fill="x")
        bar = ttk.Frame(self.content)
        bar.pack(anchor="w", pady=6)
        self.like_btn = ttk.Button(bar, command=lambda: self.react(1))
        self.like_btn.pack(side="left", padx=(0, 6))
        self.dislike_btn = ttk.Button(bar, command=lambda: self.react(-1))
        self.dislike_btn.pack(side="left")
        self.comments = tk.Text(self.content, height=6, wrap="word", state="disabled")
        self.comments.pack(fill="both", expand=True)
        row = ttk.Frame(self.content)
        row.pack(fill="x", pady=6)
        self.comment_entry = ttk.Entry(row)
        self.comment_entry.pack(side="left", fill="x", expand=True)
        self.comment_entry.bind("<Return>", lambda e: self.add_comment())
        ttk.Button(row, text="Comment", command=self.add_comment).pack(side="left", padx=6)
        self.cur_ann = None
        self.ann.bind("<<TreeviewSelect>>", lambda e: self.show_ann_detail())
        if rows:
            self.ann.selection_set(str(select_id or rows[0][0]))
        else:
            self.like_btn.config(text="Like", state="disabled")
            self.dislike_btn.config(text="Dislike", state="disabled")

    def show_ann_detail(self):
        sel = self.ann.selection()
        if not sel:
            return
        self.cur_ann = aid = int(sel[0])
        msg = db("SELECT message FROM announcements WHERE id=?", (aid,))[0][0]
        likes, dislikes = db("SELECT COALESCE(SUM(value=1),0), COALESCE(SUM(value=-1),0) "
                             "FROM reactions WHERE announcement_id=?", (aid,))[0]
        mine = db("SELECT value FROM reactions WHERE announcement_id=? AND user_id=?", (aid, self.user["id"]))
        mine = mine[0][0] if mine else 0
        self.like_btn.config(text=f"{'* ' if mine == 1 else ''}Like ({likes})", state="normal")
        self.dislike_btn.config(text=f"{'* ' if mine == -1 else ''}Dislike ({dislikes})", state="normal")
        self._set_text(self.ann_body, msg)
        lines = [f"{c} - {a}: {t}" for a, t, c in
                 db("SELECT author,text,created FROM comments WHERE announcement_id=? ORDER BY id", (aid,))]
        self._set_text(self.comments, "\n\n".join(lines) or "No comments yet.")

    @staticmethod
    def _set_text(widget, text):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.config(state="disabled")

    def react(self, value):
        if not self.cur_ann:
            return
        cur = db("SELECT value FROM reactions WHERE announcement_id=? AND user_id=?", (self.cur_ann, self.user["id"]))
        new = 0 if cur and cur[0][0] == value else value       # same click again = undo
        db("INSERT INTO reactions(announcement_id,user_id,value,synced) VALUES(?,?,?,0) "
           "ON CONFLICT(announcement_id,user_id) DO UPDATE SET value=excluded.value, synced=0",
           (self.cur_ann, self.user["id"], new))
        self.show_ann_detail()

    def add_comment(self):
        text = self.comment_entry.get().strip()[:500]
        if not text or not self.cur_ann:
            return
        db("INSERT INTO comments(announcement_id,user_id,author,text,created,synced) VALUES(?,?,?,?,?,0)",
           (self.cur_ann, self.user["id"], self.user["name"], text, now()))
        self.comment_entry.delete(0, "end")
        self.show_ann_detail()

    def new_announcement(self):
        if not self.require_admin():
            return
        win = tk.Toplevel(self)
        win.title("New announcement")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text="Message to all staff").pack(anchor="w", padx=10, pady=(10, 0))
        txt = tk.Text(win, width=60, height=8, wrap="word")
        txt.pack(padx=10, pady=6)
        mail = tk.BooleanVar(value=True)
        ttk.Checkbutton(win, text="Also email employees", variable=mail).pack(anchor="w", padx=10)

        def send():
            text = txt.get("1.0", "end").strip()
            if not text:
                return
            db("INSERT INTO announcements(author,message,created) VALUES(?,?,?)",
               (self.user["name"], text, now()))
            win.destroy()
            if mail.get():
                try:
                    sent = email_employees("New announcement", text)
                except Exception as e:                  # a mail problem must not lose the post
                    sent = False
                    print("Email failed:", e)
                if not sent:
                    messagebox.showinfo("Posted", "Posted. Email not sent (set SMTP_HOST / SMTP_USER / SMTP_PASS).")
            self.show_announcements()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="Post", command=send).pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        txt.focus_set()

    # ---- backup & restore (admin)
    def show_backup(self):
        if not self.require_admin():
            return
        self.clear("Backup & restore")
        files = sorted(BACKUP_DIR.glob("inventory_*.db")) if BACKUP_DIR.exists() else []
        lines = [f"Data folder:  {DATA_DIR}",
                 f"Local backups:  {len(files)} (latest: {files[-1].name if files else 'none yet'})",
                 f"Google Drive folder '{DRIVE_FOLDER}':  {'connected' if TOKEN.exists() else 'not connected'}",
                 f"Comments/likes waiting for a server to sync to:  {pending_sync()}"]
        for ln in lines:
            ttk.Label(self.content, text=ln).pack(anchor="w", pady=2)
        self.buttons([("Back up now", self.backup_now), ("Connect Google Drive", self.connect_drive),
                      ("Restore from file...", self.restore_file)])

    def backup_now(self):
        backup_db()
        messagebox.showinfo("Backup", "Backup saved." + ("" if TOKEN.exists() else " (Google Drive not connected, so local only.)"))
        self.show_backup()

    def connect_drive(self):
        if not CREDENTIALS.exists():
            return messagebox.showinfo(
                "One-time setup",
                "1. Google Cloud Console > new project > enable 'Google Drive API'.\n"
                "2. OAuth consent screen: External, add your Google account as a test user.\n"
                "3. Credentials > Create OAuth client ID > Desktop app > download the JSON.\n"
                f"4. Save it as credentials.json in:\n{DATA_DIR}\n"
                "5. pip install google-api-python-client google-auth-oauthlib\n\nThen press this button again.")
        try:
            drive_service()                             # opens your browser to sign in once
            backup_db()
            messagebox.showinfo("Connected", f"Backups will now upload to the '{DRIVE_FOLDER}' folder in your Drive.")
        except Exception as e:
            messagebox.showerror("Google Drive", f"Could not connect: {e}")
        self.show_backup()

    def restore_file(self):
        path = filedialog.askopenfilename(title="Choose a backup", filetypes=[("Database", "*.db"), ("All", "*.*")])
        if not path or not messagebox.askyesno("Restore", "This replaces ALL current data with the backup. Continue?"):
            return
        err = restore_from(path)
        if err:
            return messagebox.showerror("Restore failed", err)
        messagebox.showinfo("Restored", "Data restored. Please log in again.")
        self.app.user = None
        self.app.swap(LoginPage)

    def logout(self):
        threading.Thread(target=backup_db, daemon=True).start()
        self.app.user = None
        self.app.swap(LoginPage)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Modern Inventory Management")
        self.geometry("1000x640")
        self.user, self.frame = None, None
        self.swap(LoginPage)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=backup_db, daemon=True).start()
        self.after(3_600_000, self.hourly_backup)

    def swap(self, page):
        if self.frame:
            self.frame.destroy()
        self.frame = page(self)
        self.frame.pack(fill="both", expand=True)

    def hourly_backup(self):
        threading.Thread(target=backup_db, daemon=True).start()
        self.after(3_600_000, self.hourly_backup)

    def on_close(self):
        try:
            backup_db()
        except Exception as e:
            print("Backup on close failed:", e)
        self.destroy()

    def report_callback_exception(self, exc, val, tb):
        """Show errors instead of failing silently (buttons that 'do nothing')."""
        traceback.print_exception(exc, val, tb)
        messagebox.showerror("Something went wrong", f"{exc.__name__}: {val}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Modern Inventory Management")
    ap.add_argument("--recover", action="store_true", help="reset a forgotten password from the terminal")
    args = ap.parse_args()
    init_db()
    if args.recover or "recover" in Path(sys.argv[0]).stem.lower():
        recover()                                       # also runs when the exe is named ...-Recover
        input("\nPress Enter to close...")
    else:
        App().mainloop()
