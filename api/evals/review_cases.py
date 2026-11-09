"""Small pull request changes with known answers, for measuring the reviewer.

Each buggy case plants one problem; `bug_line` is its line in the new file. Clean cases should
get no bug or risk findings. Some bugs are pattern-shaped (a linter can find them) and some
need understanding (only a model can), so the set shows what each approach is good at.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    path: str
    before: str
    after: str
    bug_line: int | None  # None for a clean change
    needs_understanding: bool = False


CASES = [
    Case(
        "sql_injection",
        "app/users.py",
        before="""\
import sqlite3


def find_user(db: sqlite3.Connection, user_id: int):
    return db.execute("SELECT id, email FROM users WHERE id = ?", (user_id,)).fetchone()
""",
        after="""\
import sqlite3


def find_user(db: sqlite3.Connection, user_id: int):
    return db.execute("SELECT id, email FROM users WHERE id = ?", (user_id,)).fetchone()


def find_by_name(db: sqlite3.Connection, name: str):
    query = f"SELECT id, email FROM users WHERE name = '{name}'"
    return db.execute(query).fetchone()
""",
        bug_line=9,
    ),
    Case(
        "shell_injection",
        "app/images.py",
        before="""\
import subprocess


def thumbnail(src: str, dest: str) -> None:
    subprocess.run(["convert", src, "-resize", "128x128", dest], check=True)
""",
        after="""\
import subprocess


def thumbnail(src: str, dest: str, size: str = "128x128") -> None:
    subprocess.run(f"convert {src} -resize {size} {dest}", shell=True, check=True)
""",
        bug_line=5,
    ),
    Case(
        "eval_user_input",
        "app/calc.py",
        before="""\
from flask import Flask, request

app = Flask(__name__)
""",
        after="""\
from flask import Flask, request

app = Flask(__name__)


@app.get("/calc")
def calc():
    expression = request.args["expr"]
    return {"result": eval(expression)}
""",
        bug_line=9,
    ),
    Case(
        "hardcoded_secret",
        "app/config.py",
        before="""\
import os

SECRET_KEY = os.environ["SECRET_KEY"]
DEBUG = False
""",
        after="""\
import os

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-123")
DEBUG = False
API_PASSWORD = "hunter2-production"
""",
        bug_line=5,
    ),
    Case(
        "mutable_default",
        "app/cart.py",
        before="""\
def add_item(item: str, cart: list[str] | None = None) -> list[str]:
    cart = cart or []
    cart.append(item)
    return cart
""",
        after="""\
def add_item(item: str, cart: list[str] = []) -> list[str]:
    cart.append(item)
    return cart
""",
        bug_line=1,
    ),
    Case(
        "is_literal",
        "app/auth.py",
        before="""\
def can_delete(role: str) -> bool:
    return role == "admin"
""",
        after="""\
def can_delete(role: str) -> bool:
    if role is "admin":
        return True
    return role == "owner"
""",
        bug_line=2,
    ),
    Case(
        "swallowed_error",
        "app/billing.py",
        before="""\
def charge(gateway, order) -> None:
    gateway.charge(order.card, order.total)
    order.mark_paid()
""",
        after="""\
def charge(gateway, order) -> None:
    try:
        gateway.charge(order.card, order.total)
    except Exception:
        pass
    order.mark_paid()
""",
        bug_line=4,
        needs_understanding=True,
    ),
    Case(
        "divide_by_empty",
        "app/stats.py",
        before="""\
def total_age(users: list[dict]) -> int:
    return sum(u["age"] for u in users)
""",
        after="""\
def total_age(users: list[dict]) -> int:
    return sum(u["age"] for u in users)


def average_age(users: list[dict]) -> float:
    return total_age(users) / len(users)
""",
        bug_line=6,
        needs_understanding=True,
    ),
    Case(
        "off_by_one",
        "app/pages.py",
        before="""\
def page_titles(pages: list[dict]) -> list[str]:
    return [page["title"] for page in pages]
""",
        after="""\
def page_titles(pages: list[dict]) -> list[str]:
    titles = []
    for i in range(len(pages) + 1):
        titles.append(pages[i]["title"])
    return titles
""",
        bug_line=3,
        needs_understanding=True,
    ),
    Case(
        "inverted_permission",
        "app/posts.py",
        before="""\
def delete_post(user, post, db) -> None:
    db.delete(post)
""",
        after="""\
class Forbidden(Exception):
    pass


def delete_post(user, post, db) -> None:
    if user.id != post.author_id:
        db.delete(post)
        return
    raise Forbidden("Only the author can delete this post")
""",
        bug_line=6,
        needs_understanding=True,
    ),
    Case(
        "missing_none_check",
        "app/profile.py",
        before="""\
def display_name(db, user_id: int) -> str:
    user = db.get_user(user_id)
    if user is None:
        return "Unknown user"
    return user.name
""",
        after="""\
def display_name(db, user_id: int) -> str:
    user = db.get_user(user_id)
    return user.nickname or user.name
""",
        bug_line=3,
        needs_understanding=True,
    ),
    Case(
        "file_left_open",
        "app/settings.py",
        before="""\
import json


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
""",
        after="""\
import json


def load(path: str) -> dict:
    f = open(path)
    data = json.load(f)
    return data
""",
        bug_line=5,
    ),
    Case(
        "clean_parameterized_query",
        "app/orders.py",
        before="""\
def orders_for(db, user_id: int):
    return db.execute("SELECT * FROM orders WHERE user_id = ?", (user_id,)).fetchall()
""",
        after="""\
def orders_for(db, user_id: int, status: str | None = None):
    if status is None:
        return db.execute("SELECT * FROM orders WHERE user_id = ?", (user_id,)).fetchall()
    return db.execute(
        "SELECT * FROM orders WHERE user_id = ? AND status = ?", (user_id, status)
    ).fetchall()
""",
        bug_line=None,
    ),
    Case(
        "clean_guarded_average",
        "app/scores.py",
        before="""\
def best(scores: list[float]) -> float:
    return max(scores)
""",
        after="""\
def best(scores: list[float]) -> float:
    return max(scores)


def mean(scores: list[float]) -> float:
    if not scores:
        return 0.0
    return sum(scores) / len(scores)
""",
        bug_line=None,
    ),
    Case(
        "clean_rename",
        "app/greeting.py",
        before="""\
def greet(n: str) -> str:
    return "Hello, " + n
""",
        after="""\
def greet(name: str) -> str:
    \"\"\"Return a friendly greeting for the given name.\"\"\"
    return f"Hello, {name}"
""",
        bug_line=None,
    ),
]
