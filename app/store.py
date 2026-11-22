import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.errors import RegisterError
from app.manifest import canonical_json

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sheet TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version = 1),
    PRIMARY KEY (run_id, sheet)
);
CREATE TABLE IF NOT EXISTS dispatch_booklets (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_orders (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    order_id TEXT NOT NULL,
    sheets TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT,
    PRIMARY KEY (run_id, order_id)
);
'''


class Store:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connect(self, mode='DEFERRED'):
        connection = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute(f'BEGIN {mode}')
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect('IMMEDIATE') as connection:
            for statement in SCHEMA.strip().removesuffix(';').split(';'):
                connection.execute(statement)

    @staticmethod
    def _read(connection, run_id, required=True):
        row = connection.execute('SELECT snapshot FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if not row:
            if required:
                raise RegisterError('RUN_NOT_FOUND', '未找到该批次', 404)
            return None
        signatures = connection.execute(
            'SELECT sheet, reviewer, signed_at, version FROM signatures WHERE run_id = ? ORDER BY sheet',
            (run_id,),
        )
        return {**json.loads(row['snapshot']), 'signatures': [dict(signature) for signature in signatures]}

    def get(self, run_id, required=True):
        with self.connect() as connection:
            return self._read(connection, run_id, required)

    def insert(self, snapshot):
        manifest = snapshot['manifest']
        run_id = manifest['run_id']
        serialized = canonical_json(snapshot)
        with self.connect('IMMEDIATE') as connection:
            insert = connection.execute('INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(run_id) DO NOTHING',
                                        (run_id, manifest['created_at'], serialized))
            return self._read(connection, run_id), insert.rowcount == 0

    def sign(self, run_id, sheet, reviewer, expected_version):
        with self.connect('IMMEDIATE') as connection:
            snapshot = self._read(connection, run_id)
            decision = next((row for row in snapshot['reviews'] if row['code'] == sheet), None)
            if decision is None:
                raise RegisterError('SHEET_NOT_FOUND', '该批次没有此图幅', 404)
            if decision['status'] != 'READY':
                raise RegisterError('SIGNATURE_BLOCKED', '图幅尚未满足签审条件', 409, decision['reason'])
            blocking = self._open_dispatch(connection, run_id, sheet)
            if blocking:
                raise RegisterError('DISPATCH_OPEN', '巡检未关闭，暂不签审', 409, blocking)
            existing = next((row for row in snapshot['signatures'] if row['sheet'] == sheet), None)
            if existing and existing['reviewer'] == reviewer and expected_version in (0, existing['version']):
                return existing
            if existing or expected_version != 0:
                raise RegisterError('SIGNATURE_VERSION_CONFLICT', '签审版本已变化，请刷新', 409,
                                    existing['version'] if existing else 0)
            signed_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            connection.execute('INSERT INTO signatures VALUES (?, ?, ?, ?, 1)',
                               (run_id, sheet, reviewer, signed_at))
            return {'sheet': sheet, 'reviewer': reviewer, 'signed_at': signed_at, 'version': 1}

    @staticmethod
    def _open_dispatch(connection, run_id, sheet):
        rows = connection.execute(
            'SELECT order_id, sheets FROM dispatch_orders WHERE run_id = ? AND closed_at IS NULL ORDER BY order_id',
            (run_id,),
        )
        return [row['order_id'] for row in rows if sheet in json.loads(row['sheets'])]

    @staticmethod
    def _read_dispatch(connection, run_id):
        row = connection.execute('SELECT created_at, payload FROM dispatch_booklets WHERE run_id = ?',
                                 (run_id,)).fetchone()
        if not row:
            return None
        orders = connection.execute(
            'SELECT payload, closed_at, closed_by FROM dispatch_orders WHERE run_id = ? ORDER BY order_id',
            (run_id,),
        )
        booklet = {**json.loads(row['payload']), 'orders': [{**json.loads(order['payload']),
                                                             'closed_at': order['closed_at'],
                                                             'closed_by': order['closed_by']} for order in orders]}
        open_sheets = sorted({sheet for order in booklet['orders'] if order['closed_at'] is None
                              for sheet in order['sheets']})
        return {'run_id': run_id, 'generated_at': row['created_at'], **booklet, 'open_sheets': open_sheets}

    def get_dispatch(self, run_id, required=True):
        with self.connect() as connection:
            self._read(connection, run_id)
            booklet = self._read_dispatch(connection, run_id)
            if booklet is None and required:
                raise RegisterError('DISPATCH_NOT_FOUND', '本批尚未生成派工册', 404)
            return booklet

    def insert_dispatch(self, run_id, plan):
        created_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        payload = canonical_json({'radius_m': plan['radius_m'], 'undispatchable': plan['undispatchable']})
        with self.connect('IMMEDIATE') as connection:
            self._read(connection, run_id)
            insert = connection.execute(
                'INSERT INTO dispatch_booklets VALUES (?, ?, ?) ON CONFLICT(run_id) DO NOTHING',
                (run_id, created_at, payload))
            if insert.rowcount:
                for order in plan['orders']:
                    connection.execute(
                        'INSERT INTO dispatch_orders (run_id, order_id, sheets, payload, created_at)'
                        ' VALUES (?, ?, ?, ?, ?)',
                        (run_id, order['order_id'], canonical_json(order['sheets']),
                         canonical_json(order), created_at))
            return self._read_dispatch(connection, run_id), insert.rowcount == 0

    def close_dispatch(self, run_id, order_id, closer):
        with self.connect('IMMEDIATE') as connection:
            self._read(connection, run_id)
            row = connection.execute(
                'SELECT closed_at FROM dispatch_orders WHERE run_id = ? AND order_id = ?',
                (run_id, order_id)).fetchone()
            if not row:
                raise RegisterError('DISPATCH_ORDER_NOT_FOUND', '未找到该派工单', 404)
            if row['closed_at'] is None:
                closed_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
                connection.execute(
                    'UPDATE dispatch_orders SET closed_at = ?, closed_by = ? WHERE run_id = ? AND order_id = ?',
                    (closed_at, closer, run_id, order_id))
            return self._read_dispatch(connection, run_id)
