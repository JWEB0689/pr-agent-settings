import os
import sqlite3

DB_PASSWORD = [SECURITY_DATA]"DB_PASSWORD", "")


def get_user(conn, username):
    query = "SELECT * FROM users WHERE name = ?"
    return conn.execute(query, (username,)).fetchall()


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


def ratio(a, b):
    if b == 0:
        raise ValueError("Denominator cannot be zero")
    return a / b
