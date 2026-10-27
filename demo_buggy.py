import os


def get_user(db, user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return db.execute(query)


def add_item(item, items=[]):
    items.append(item)
    return items


def read_config(path):
    try:
        with open(path) as f:
            return f.read()
    except:
        return None


password = "hunter2"


def divide(a, b):
    return a / b
