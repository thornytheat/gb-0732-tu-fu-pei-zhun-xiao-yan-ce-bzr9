from concurrent.futures import ThreadPoolExecutor
import math
import sqlite3

import pytest

from app.config import DISPATCH_GROUP_RADIUS_M, RunOptions
from app.errors import RegisterError
from app.modules.dispatch import service
from app.modules.dispatch.plan import plan_dispatch
from app.modules.dispatch.service import generate_dispatch
from app.modules.review.service import run_review
from app.store import Store
from conftest import by_key


def make_run(client, payload=None):
    return client.post('/api/review/runs', json=payload or {}).json()['manifest']['run_id']


def test_dispatch_groups_only_nearby_anomalies_and_never_duplicates_cross_sheet(client):
    run_id = make_run(client)
    response = client.post(f'/api/review/runs/{run_id}/dispatch')
    booklet = response.json()
    assert response.status_code == 200 and booklet['reused'] is False
    assert booklet['radius_m'] == DISPATCH_GROUP_RADIUS_M
    assert len(booklet['orders']) == 3
    keys = [item['key'] for order in booklet['orders'] for item in order['items']]
    assert len(keys) == len(set(keys)) == 7  # 六站超限见证加一处离群，跨图幅只计一次
    seam_items = [item for order in booklet['orders'] for item in order['items'] if item['kind'] == 'SEAM_GAP']
    assert all(item['sheets'] == ['0421', '0422'] for item in seam_items)
    assert {item['name'] for item in seam_items} == {f'HX-QS-0{index}' for index in range(1, 7)}
    for order in booklet['orders']:
        assert order['span_m'] <= DISPATCH_GROUP_RADIUS_M + 1e-9
        assert order['order_id'].startswith(run_id[:12])
    mixed = next(order for order in booklet['orders'] if len(order['items']) == 3)
    assert {item['name'] for item in mixed['items']} == {'P08', 'HX-QS-05', 'HX-QS-06'}
    outlier = next(item for item in mixed['items'] if item['kind'] == 'CONTROL_OUTLIER')
    assert outlier['sheets'] == ['0421'] and outlier['position'] is not None
    assert [anchor['name'] for anchor in outlier['adjacent_controls']] == ['P09', 'P07', 'P06']
    assert booklet['open_sheets'] == ['0421', '0422']
    assert booklet['undispatchable'] == []


def test_regeneration_and_rerun_never_create_a_second_booklet(client, settings):
    run_id = make_run(client)
    first = client.post(f'/api/review/runs/{run_id}/dispatch').json()
    again = client.post(f'/api/review/runs/{run_id}/dispatch').json()
    assert again['reused'] is True
    assert again['generated_at'] == first['generated_at']
    rerun = client.post('/api/review/runs', json={}).json()
    assert rerun['reused'] is True and rerun['manifest']['run_id'] == run_id
    third = client.post(f"/api/review/runs/{run_id}/dispatch").json()
    assert third['reused'] is True and third['generated_at'] == first['generated_at']
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM dispatch_booklets').fetchone() == (1,)
        assert connection.execute('SELECT COUNT(*) FROM dispatch_orders').fetchone() == (3,)


def test_concurrent_generation_stores_exactly_one_booklet(settings):
    store = Store(settings.database)
    store.initialize()
    snapshot, _ = run_review(settings, store, RunOptions())
    run_id = snapshot['manifest']['run_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: generate_dispatch(store, run_id), range(4)))
    assert sum(not reused for _, reused in results) == 1
    assert len({booklet['generated_at'] for booklet, _ in results}) == 1
    assert all(len(booklet['orders']) == 3 for booklet, _ in results)
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM dispatch_booklets').fetchone() == (1,)


def test_interrupted_generation_leaves_no_partial_booklet_and_old_version_readable(settings, monkeypatch):
    store = Store(settings.database)
    store.initialize()
    snapshot, _ = run_review(settings, store, RunOptions())
    run_id = snapshot['manifest']['run_id']
    plan = plan_dispatch(snapshot)
    broken = {'radius_m': plan['radius_m'], 'undispatchable': plan['undispatchable'],
              'orders': [{**plan['orders'][0], 'span_m': math.nan}, *plan['orders'][1:]]}
    monkeypatch.setattr(service, 'plan_dispatch', lambda _snapshot: broken)
    with pytest.raises(ValueError):
        generate_dispatch(store, run_id)
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM dispatch_booklets').fetchone() == (0,)
        assert connection.execute('SELECT COUNT(*) FROM dispatch_orders').fetchone() == (0,)
    monkeypatch.undo()
    booklet, reused = generate_dispatch(store, run_id)
    assert reused is False and len(booklet['orders']) == 3
    store.close_dispatch(run_id, booklet['orders'][0]['order_id'], '李外业')
    monkeypatch.setattr(service, 'plan_dispatch', lambda _snapshot: 1 / 0)
    kept, reused = generate_dispatch(store, run_id)  # 旧版已存在时不再重排，中断也不会波及
    assert reused is True and kept['generated_at'] == booklet['generated_at']
    monkeypatch.undo()
    readable = store.get_dispatch(run_id)
    assert readable['orders'][0]['closed_by'] == '李外业'
    assert readable['generated_at'] == booklet['generated_at']


def test_open_dispatch_blocks_signature_until_closed(client):
    run_id = make_run(client, {'seam_limit_m': 3})  # 拼缝放宽后 0421 可签，但仍有离群派工单
    assert by_key(client.get(f'/api/review/runs/{run_id}').json()['reviews'], '0421')['status'] == 'READY'
    booklet = client.post(f'/api/review/runs/{run_id}/dispatch').json()
    assert booklet['open_sheets'] == ['0421']
    url = f'/api/review/runs/{run_id}/signatures'
    blocked = client.post(url, json={'sheet': '0421', 'reviewer': '周测绘'})
    assert blocked.status_code == 409
    assert blocked.json()['detail']['code'] == 'DISPATCH_OPEN'
    assert blocked.json()['detail']['evidence'] == [booklet['orders'][0]['order_id']]
    order_id = booklet['orders'][0]['order_id']
    closed = client.post(f'/api/review/runs/{run_id}/dispatch/{order_id}/closure', json={'closer': ' 李外业 '})
    assert closed.status_code == 200
    assert closed.json()['orders'][0]['closed_by'] == '李外业'
    assert closed.json()['open_sheets'] == []
    assert client.post(url, json={'sheet': '0421', 'reviewer': '周测绘'}).status_code == 200


def test_closure_is_idempotent_and_validated(client):
    run_id = make_run(client)
    booklet = client.post(f'/api/review/runs/{run_id}/dispatch').json()
    order_id = booklet['orders'][0]['order_id']
    url = f'/api/review/runs/{run_id}/dispatch/{order_id}/closure'
    first = client.post(url, json={'closer': '李外业'}).json()
    again = client.post(url, json={'closer': '陈复核'}).json()
    first_order, again_order = first['orders'][0], again['orders'][0]
    assert (again_order['closed_at'], again_order['closed_by']) == (first_order['closed_at'], '李外业')
    assert client.post(url, json={'closer': '李外业', 'note': '多字段'}).status_code == 422
    assert client.post(url, json={'closer': '李\t外业'}).status_code == 422
    missing = client.post(f'/api/review/runs/{run_id}/dispatch/{run_id[:12]}-99/closure', json={'closer': '李外业'})
    assert missing.status_code == 404 and missing.json()['detail']['code'] == 'DISPATCH_ORDER_NOT_FOUND'


def test_dispatch_requires_a_run_and_a_generated_booklet(client):
    run_id = make_run(client)
    not_generated = client.get(f'/api/review/runs/{run_id}/dispatch')
    assert not_generated.status_code == 404 and not_generated.json()['detail']['code'] == 'DISPATCH_NOT_FOUND'
    unknown = client.get(f"/api/review/runs/{'0' * 64}/dispatch")
    assert unknown.status_code == 404 and unknown.json()['detail']['code'] == 'RUN_NOT_FOUND'
    assert client.post(f"/api/review/runs/{'0' * 64}/dispatch").status_code == 404
    assert client.post(f"/api/review/runs/{'0' * 64}/dispatch/{'0' * 12}-01/closure",
                       json={'closer': '李外业'}).status_code == 404


def synthetic_snapshot(outliers=(), edges=(), accepted=(), models=None):
    return {
        'manifest': {'run_id': 'a' * 64},
        'refit': {'outliers': list(outliers), 'accepted': list(accepted), 'sheets': models or {}},
        'seams': {'edges': list(edges)},
    }


def outlier(sheet, name, east, north, projected=True):
    point = {'sheet': sheet, 'name': name, 'ground': [east + 3, north + 4],
             'before': {'distance_m': 9.0}, 'after': {'distance_m': 15.0},
             'pixel_source': {'id': f'pixels.csv:{name}', 'file': 'pixels.csv', 'line': 2},
             'ground_source': {'id': f'ground.csv:{name}', 'file': 'ground.csv', 'line': 2}}
    if projected:
        point['after']['projected'] = [east, north]
    return point


def fail_edge(stations):
    return {'status': 'FAIL', 'code': 'HX-QS', 'a': '0421', 'b': '0422', 'a_side': 'E', 'b_side': 'W',
            'limit_m': 1.5, 'stations': list(stations)}


def station(name, east, north, gap=2.5):
    return {'name': name, 'gap_m': gap, 'at': 0.5, 'a_world': [east - 1, north], 'b_world': [east + 1, north],
            'source': {'id': f'witness.csv:{name}', 'file': 'witness.csv', 'line': 2}}


def test_far_apart_records_are_not_forced_into_one_order():
    snapshot = synthetic_snapshot(outliers=[outlier('0421', 'P08', 0, 0), outlier('0421', 'P09', 500, 0)],
                                  models={'0421': {'status': 'FIT'}})
    plan = plan_dispatch(snapshot, radius_m=100)
    assert [len(order['items']) for order in plan['orders']] == [1, 1]
    assert plan['orders'][0]['order_id'] != plan['orders'][1]['order_id']
    again = plan_dispatch(snapshot, radius_m=100)
    assert again == plan


def test_complete_linkage_never_chains_distant_records():
    snapshot = synthetic_snapshot(outliers=[outlier('0421', 'A', 0, 0), outlier('0421', 'B', 90, 0),
                                            outlier('0421', 'C', 180, 0)],
                                  models={'0421': {'status': 'FIT'}})
    plan = plan_dispatch(snapshot, radius_m=100)
    sizes = sorted(len(order['items']) for order in plan['orders'])
    assert sizes == [1, 2]  # A-B 相近、B-C 相近，但 A-C 相距 180 m，不得同单
    pair = next(order for order in plan['orders'] if len(order['items']) == 2)
    assert {item['name'] for item in pair['items']} == {'A', 'B'}


def test_records_without_reliable_position_are_undispatchable_with_reasons():
    edge = fail_edge([{**station('HX-QS-01', 0, 0), 'a_world': None, 'b_world': None}])
    snapshot = synthetic_snapshot(outliers=[outlier('0421', 'P08', 0, 0, projected=False),
                                            outlier('0432', 'P01', 10, 10)],
                                  edges=[edge], models={'0421': {'status': 'FIT'}, '0432': {'status': 'BLOCKED'}})
    plan = plan_dispatch(snapshot, radius_m=100)
    assert plan['orders'] == []
    reasons = {item['name']: item['reason'] for item in plan['undispatchable']}
    assert reasons == {'P08': 'NO_POSITION_BASIS', 'P01': 'MODEL_UNAVAILABLE', 'HX-QS-01': 'NO_POSITION_BASIS'}
    assert all(item['sheets'] for item in plan['undispatchable'])


def test_only_over_limit_witnesses_are_dispatched_and_edge_is_one_record():
    edge = fail_edge([station('HX-QS-01', 0, 0, gap=2.5), station('HX-QS-02', 10, 0, gap=1.2)])
    plan = plan_dispatch(synthetic_snapshot(edges=[edge]), radius_m=100)
    items = [item for order in plan['orders'] for item in order['items']]
    assert len(items) == 1
    assert items[0]['name'] == 'HX-QS-01' and items[0]['sheets'] == ['0421', '0422']
    assert items[0]['adjacent_edge']['code'] == 'HX-QS'
    assert [row['name'] for row in items[0]['adjacent_stations']] == ['HX-QS-02']


def test_non_fail_edges_and_clean_batches_produce_no_orders():
    passing = {**fail_edge([station('HX-QS-01', 0, 0)]), 'status': 'PASS'}
    plan = plan_dispatch(synthetic_snapshot(edges=[passing]))
    assert plan['orders'] == [] and plan['undispatchable'] == []
