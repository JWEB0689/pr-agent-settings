import sqlite3
import os

DB_PASSWORD = os.getenv("DB_PASSWORD")


def get_user(conn, username):
    query = "SELECT * FROM users WHERE name = ?"
    return conn.execute(query, (username,)).fetchall()
    return conn.execute(query).fetchall()


def add_item(item, items=None):
    if items is None:
        items = []
    items.append(item)
    return items


def read_config(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None
        return None


def ratio(a, b):
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
