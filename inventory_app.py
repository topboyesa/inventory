"""Modern Inventory Management - tkinter + SQLite starter.
Run:  python inventory_app.py
First account registered becomes the admin.
"""
import hashlib
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import tkinter as tk
from datetime import datetime
from email.message import EmailMessage
from tkinter import messagebox, simpledialog, ttk

DB = "inventory.db"
BACKUP_DIR = "backups"
COLORS = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
          "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]


# ---------------------------------------------------------------- database
def db(sql, params=()):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def init_db():
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
    """)
    # upgrade databases created before buyer details existed
    have = [r[1] for r in con.execute("PRAGMA table_info(sales)")]
    for col in ("buyer_name", "buyer_phone"):
        if col not in have:
            con.execute(f"ALTER TABLE sales ADD COLUMN {col} TEXT")
    if not con.execute("SELECT 1 FROM products").fetchone():
        con.executemany("INSERT INTO products(name,quantity,price) VALUES(?,?,?)",
                        [(f"test_product{i}", 20 * i, 100.0 * i) for i in range(1, 5)])
    con.commit()
    con.close()


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------- backups & email
def online():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2).close()
        return True
    except OSError:
        return False


def upload_to_cloud(path):
    """TODO: implement with google-cloud-storage, e.g.
    storage.Client().bucket("my-bucket").blob(os.path.basename(path)).upload_from_filename(path)
    Return True on success."""
    return False


def backup_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"inventory_{datetime.now():%Y%m%d}.db")
    if not os.path.exists(dest):                      # one backup per day
        src, dst = sqlite3.connect(DB), sqlite3.connect(dest)
        src.backup(dst)
        src.close()
        dst.close()
    if online():                                      # retry anything not yet uploaded
        for f in os.listdir(BACKUP_DIR):
            p = os.path.join(BACKUP_DIR, f)
            if f.endswith(".db") and not os.path.exists(p + ".uploaded"):
                if upload_to_cloud(p):
                    open(p + ".uploaded", "w").close()


def email_employees(subject, body):
    host, user, pw = (os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
    if not (host and user and pw):
        return False
    to = [r[0] for r in db("SELECT email FROM users")]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


# --------------------------------------------------------------------- UI
def make_table(parent, cols, widths):
    tree = ttk.Treeview(parent, columns=cols, show="headings", height=14)
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
        box = ttk.Frame(self)
        box.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(box, text="Modern Inventory Management", font=("Segoe UI", 18, "bold")).pack(pady=10)
        ttk.Label(box, text="Login").pack()
        ttk.Label(box, text="Email").pack(anchor="w")
        self.email = ttk.Entry(box, width=32)
        self.email.pack(pady=2)
        ttk.Label(box, text="Password").pack(anchor="w")
        self.pw = ttk.Entry(box, width=32, show="*")
        self.pw.pack(pady=2)
        self.pw.bind("<Return>", lambda e: self.login())
        ttk.Button(box, text="Login", command=self.login).pack(fill="x", pady=(10, 2))
        ttk.Button(box, text="Create account", command=self.register).pack(fill="x", pady=2)
        ttk.Button(box, text="Login with Google", command=lambda: messagebox.showinfo(
            "Coming soon", "Google sign-in needs an OAuth client; see notes.")).pack(fill="x", pady=2)

    def login(self):
        row = db("SELECT id,name,email,salt,pw_hash,role FROM users WHERE email=?",
                 (self.email.get().strip().lower(),))
        if row and secrets.compare_digest(row[0][4], hash_pw(self.pw.get(), row[0][3])):
            self.app.user = {"id": row[0][0], "name": row[0][1], "email": row[0][2], "role": row[0][5]}
            self.app.swap(MainPage)
        else:
            messagebox.showerror("Login failed", "Wrong email or password.")

    def register(self):
        email, pw = self.email.get().strip().lower(), self.pw.get()
        if "@" not in email or len(pw) < 6:
            return messagebox.showwarning("Check details", "Valid email and a 6+ character password.")
        name = simpledialog.askstring("Create account", "Your name:")
        if not name:
            return
        role = "admin" if not db("SELECT 1 FROM users") else "employee"
        salt = secrets.token_hex(16)
        try:
            db("INSERT INTO users(name,email,salt,pw_hash,role) VALUES(?,?,?,?,?)",
               (name, email, salt, hash_pw(pw, salt), role))
            messagebox.showinfo("Done", f"Account created ({role}). You can log in now.")
        except sqlite3.IntegrityError:
            messagebox.showerror("Exists", "That email is already registered.")


class MainPage(ttk.Frame):
    def __init__(self, app):
        super().__init__(app)
        self.app, self.user = app, app.user
        self.is_admin = self.user["role"] == "admin"

        side = ttk.Frame(self, padding=10)
        side.pack(side="left", fill="y")
        av = tk.Canvas(side, width=70, height=70, highlightthickness=0)
        av.create_oval(2, 2, 68, 68, fill="#4e79a7", outline="")
        av.create_text(35, 35, text=self.user["name"][:1].upper(), fill="white", font=("Segoe UI", 24, "bold"))
        av.pack()  # swap for a real photo later (PIL ImageTk)
        ttk.Label(side, text=f"Hello {self.user['name']}").pack(pady=(4, 0))
        ttk.Label(side, text=self.user["role"].title(), foreground="gray").pack(pady=(0, 12))
        for text, fn in [("Inventory", self.show_inventory), ("Orders", self.show_orders),
                         ("Sales", self.show_sales), ("Employees", self.show_employees),
                         ("Announcements", self.show_announcements), ("Logout", self.logout)]:
            ttk.Button(side, text=text, command=fn, width=18).pack(pady=3)

        self.content = ttk.Frame(self, padding=15)
        self.content.pack(side="left", fill="both", expand=True)
        self.show_inventory()

    def clear(self, title):
        for w in self.content.winfo_children():
            w.destroy()
        ttk.Label(self.content, text=title, font=("Segoe UI", 16, "bold")).pack(anchor="w")

    def buttons(self, items):
        bar = ttk.Frame(self.content)
        bar.pack(anchor="w")
        for text, fn in items:
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=(0, 6))

    # ---- inventory
    def show_inventory(self):
        self.clear("Inventory")
        self.inv = make_table(self.content, ("id", "name", "quantity", "price"), (50, 250, 100, 100))
        self.inv.tag_configure("low", foreground="red")
        for r in db("SELECT id,name,quantity,price FROM products ORDER BY name"):
            self.inv.insert("", "end", values=r, tags=("low",) if r[2] <= 5 else ())
        items = [("Create order", self.create_order)]
        if self.is_admin:
            items += [("Add product", self.add_product), ("Set quantity", self.set_qty),
                      ("Delete", self.del_product)]
        self.buttons(items)

    def add_product(self):
        name = simpledialog.askstring("Add product", "Name:")
        qty = simpledialog.askinteger("Add product", "Quantity:", minvalue=0) if name else None
        price = simpledialog.askfloat("Add product", "Price:", minvalue=0) if qty is not None else None
        if price is not None:
            try:
                db("INSERT INTO products(name,quantity,price) VALUES(?,?,?)", (name, qty, price))
            except sqlite3.IntegrityError:
                messagebox.showerror("Exists", "Product name already exists.")
            self.show_inventory()

    def set_qty(self):
        row = selected(self.inv)
        if row:
            q = simpledialog.askinteger("Quantity", f"New quantity for {row[1]}:", minvalue=0)
            if q is not None:
                db("UPDATE products SET quantity=? WHERE id=?", (q, row[0]))
                self.show_inventory()

    def del_product(self):
        row = selected(self.inv)
        if row and messagebox.askyesno("Delete", f"Delete {row[1]}?"):
            db("DELETE FROM products WHERE id=?", (row[0],))
            self.show_inventory()

    def create_order(self):
        row = selected(self.inv)
        if not row:
            return messagebox.showinfo("Select", "Select a product first.")
        q = simpledialog.askinteger("Order", f"Quantity of {row[1]}:", minvalue=1)
        if q:
            db("INSERT INTO orders(product_id,product_name,qty,created) VALUES(?,?,?,?)",
               (row[0], row[1], q, now()))
            messagebox.showinfo("Order", "Order added to Orders.")

    # ---- orders
    def show_orders(self):
        self.clear("Pending orders")
        self.ord = make_table(self.content, ("id", "product", "qty", "created"), (50, 250, 80, 150))
        for r in db("SELECT id,product_name,qty,created FROM orders WHERE status='pending' ORDER BY id"):
            self.ord.insert("", "end", values=r)
        self.buttons([("Complete sale", self.complete_sale), ("Cancel order", self.cancel_order)])

    def complete_sale(self):
        row = selected(self.ord)
        if not row:
            return
        oid, name, qty = row[0], row[1], int(row[2])
        p = db("SELECT id,quantity,price FROM products WHERE name=?", (name,))
        if not p:
            return messagebox.showerror("Missing", "Product no longer exists.")
        if p[0][1] < qty:
            return messagebox.showerror("Stock", f"Only {p[0][1]} in stock.")
        total = qty * p[0][2]
        dlg = BuyerDialog(self, f"{qty} x {name} = {total:,.2f}")
        if not dlg.result:                      # cancelled: leave the order pending
            return
        buyer_name, buyer_phone = dlg.result
        db("UPDATE products SET quantity=quantity-? WHERE id=?", (qty, p[0][0]))
        db("INSERT INTO sales(product_name,qty,total,sold_at,buyer_name,buyer_phone) VALUES(?,?,?,?,?,?)",
           (name, qty, total, now(), buyer_name, buyer_phone))
        db("UPDATE orders SET status='completed' WHERE id=?", (oid,))
        self.show_orders()

    def cancel_order(self):
        row = selected(self.ord)
        if row:
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
            self.buttons([("Toggle admin", self.toggle_role)])

    def toggle_role(self):
        row = selected(self.emp)
        if row and row[0] != self.user["id"]:
            new = "employee" if row[3] == "admin" else "admin"
            db("UPDATE users SET role=? WHERE id=?", (new, row[0]))
            self.show_employees()

    # ---- announcements
    def show_announcements(self):
        self.clear("Announcements")
        if self.is_admin:
            self.msg = tk.Text(self.content, height=4, width=70)
            self.msg.pack(anchor="w", pady=6)
            self.buttons([("Post & email", self.post)])
        feed = tk.Text(self.content, height=16, state="normal", wrap="word")
        for author, message, created in db("SELECT author,message,created FROM announcements ORDER BY id DESC"):
            feed.insert("end", f"{created} - {author}\n{message}\n\n")
        feed.config(state="disabled")
        feed.pack(fill="both", expand=True, pady=8)

    def post(self):
        text = self.msg.get("1.0", "end").strip()
        if not text:
            return
        db("INSERT INTO announcements(author,message,created) VALUES(?,?,?)",
           (self.user["name"], text, now()))
        try:
            sent = email_employees("New announcement", text)
        except Exception as e:  # network/SMTP problems shouldn't lose the post
            sent = False
            print("Email failed:", e)
        if not sent:
            messagebox.showinfo("Posted", "Posted. Email not sent (set SMTP_HOST/SMTP_USER/SMTP_PASS).")
        self.show_announcements()

    def logout(self):
        backup_db()
        self.app.user = None
        self.app.swap(LoginPage)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Modern Inventory Management")
        self.geometry("1000x620")
        self.user, self.frame = None, None
        self.swap(LoginPage)
        self.after(3_600_000, self.hourly_backup)

    def swap(self, page):
        if self.frame:
            self.frame.destroy()
        self.frame = page(self)
        self.frame.pack(fill="both", expand=True)

    def hourly_backup(self):
        backup_db()
        self.after(3_600_000, self.hourly_backup)


if __name__ == "__main__":
    init_db()
    backup_db()
    App().mainloop()
