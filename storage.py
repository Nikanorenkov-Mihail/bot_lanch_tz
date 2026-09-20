"""Локальное хранение заказов в SQLite (файл лежит в /app/data — монтируется как volume в Docker)."""

import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("data/orders.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_date TEXT NOT NULL,
            tg_id TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            category TEXT NOT NULL,
            dish TEXT,
            price INTEGER,
            PRIMARY KEY (order_date, tg_id, category)
        )
        """
    )
    return conn


def set_order_item(tg_id: int, name: str, category: str, dish, price) -> None:
    """dish=None означает, что сотрудник пропустил эту категорию."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO orders
                (order_date, tg_id, employee_name, category, dish, price)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (date.today().isoformat(), str(tg_id), name, category, dish, price),
        )


def clear_today_order(tg_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM orders WHERE order_date = ? AND tg_id = ?",
            (date.today().isoformat(), str(tg_id)),
        )


def get_today_orders():
    """Все строки за сегодня: (tg_id, employee_name, category, dish, price)."""
    with _conn() as conn:
        cur = conn.execute(
            "SELECT tg_id, employee_name, category, dish, price FROM orders WHERE order_date = ?",
            (date.today().isoformat(),),
        )
        return cur.fetchall()


def get_employee_today_order(tg_id: int):
    """Выбор конкретного сотрудника за сегодня: [(category, dish, price), ...]."""
    with _conn() as conn:
        cur = conn.execute(
            "SELECT category, dish, price FROM orders WHERE order_date = ? AND tg_id = ?",
            (date.today().isoformat(), str(tg_id)),
        )
        return cur.fetchall()
