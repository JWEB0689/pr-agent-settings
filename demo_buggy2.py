import sqlite3

DB_PASSWORD = "hunter2-secret"


def get_user(conn, username):
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    return conn.execute(query).fetchall()


def add_item(item, items=[]):
    items.append(item)
    return items


def read_config(path):
    try:
        with open(path) as f:
            return f.read()
    except:
        return None


def ratio(a, b):
    return a / b
