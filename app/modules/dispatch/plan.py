import math
from collections import defaultdict

from app.config import DISPATCH_GROUP_RADIUS_M, DISPATCH_MAX_ANCHORS
from app.modules.seams.geometry import DISTANCE_EPSILON_M

# 派工候选只取本批离群控制点与超限接缝见证；同一处异常只生成一条记录，
# 责任图幅可以不止一幅，但记录不随图幅重复。
def outlier_candidates(refit):
    accepted = defaultdict(list)
    for row in refit['accepted']:
        accepted[row['sheet']].append(row)
    candidates = []
    for row in refit['outliers']:
        sheet = row['sheet']
        projected = (row.get('after') or {}).get('projected')
        basis = projected or row.get('ground')
        anchors = sorted(accepted[sheet], key=lambda point: math.dist(basis, point['ground']))[:DISPATCH_MAX_ANCHORS] \
            if basis else []
        candidate = {
            'key': f"control:{sheet}:{row['name']}",
            'kind': 'CONTROL_OUTLIER',
            'sheets': [sheet],
            'name': row['name'],
            'position': list(projected) if projected else None,
            'detail': {
                'sheet': sheet,
                'name': row['name'],
                'residual_m': row['after']['distance_m'] if row.get('after') else None,
                'initial_m': row['before']['distance_m'] if row.get('before') else None,
                'registered_ground_m': list(row['ground']) if row.get('ground') else None,
                'projected_m': list(projected) if projected else None,
                'pixel_source': row.get('pixel_source'),
                'ground_source': row.get('ground_source'),
            },
            'adjacent_controls': [{'name': point['name'], 'sheet': sheet,
                                   'distance_m': math.dist(basis, point['ground'])} for point in anchors],
        }
        if refit['sheets'].get(sheet, {}).get('status') != 'FIT':
            candidate.update(position=None, reason='MODEL_UNAVAILABLE')
        elif not projected:
            candidate.update(position=None, reason='NO_POSITION_BASIS')
        candidates.append(candidate)
    return candidates


def seam_candidates(seams):
    candidates = []
    for edge in seams['edges']:
        if edge['status'] != 'FAIL':
            continue
        stations = edge.get('stations', [])
        for index, station in enumerate(stations):
            gap = station.get('gap_m')
            if gap is None or gap <= edge['limit_m'] + DISTANCE_EPSILON_M:
                continue
            a_world, b_world = station.get('a_world'), station.get('b_world')
            position = [(a_world[0] + b_world[0]) / 2, (a_world[1] + b_world[1]) / 2] \
                if a_world and b_world else None
            neighbors = [stations[index + offset] for offset in (-1, 1) if 0 <= index + offset < len(stations)]
            candidate = {
                'key': f"seam:{edge['code']}:{station['source']['id']}",
                'kind': 'SEAM_GAP',
                'sheets': [edge['a'], edge['b']],
                'name': station['name'],
                'position': position,
                'detail': {
                    'edge': edge['code'],
                    'station': station['name'],
                    'gap_m': gap,
                    'limit_m': edge['limit_m'],
                    'a_world': list(a_world) if a_world else None,
                    'b_world': list(b_world) if b_world else None,
                    'source': station.get('source'),
                },
                'adjacent_edge': {'code': edge['code'], 'a': edge['a'], 'a_side': edge['a_side'],
                                  'b': edge['b'], 'b_side': edge['b_side']},
                'adjacent_stations': [{'name': row['name'], 'at': row['at'], 'gap_m': row.get('gap_m')}
                                      for row in neighbors],
            }
            if position is None:
                candidate['reason'] = 'NO_POSITION_BASIS'
            candidates.append(candidate)
    return candidates


def _linkage(cluster, other):
    return max(math.dist(a['position'], b['position']) for a in cluster for b in other)


def group_orders(candidates, radius_m):
    # 全连接凝聚：并类后类内任意两条记录间距都不超过半径，相隔较远的记录不会硬凑进一张单。
    clusters = [[candidate] for candidate in
                sorted(candidates, key=lambda item: (item['position'][0], item['position'][1], item['key']))]
    while True:
        nearest = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                distance = _linkage(clusters[i], clusters[j])
                if nearest is None or distance < nearest[0] - DISTANCE_EPSILON_M:
                    nearest = (distance, i, j)
        if nearest is None or nearest[0] > radius_m + DISTANCE_EPSILON_M:
            break
        _, i, j = nearest
        clusters[i] = sorted(clusters[i] + clusters[j], key=lambda item: item['key'])
        del clusters[j]
    clusters.sort(key=lambda cluster: (min(item['position'][0] for item in cluster),
                                       min(item['position'][1] for item in cluster), cluster[0]['key']))
    return clusters


def _order(run_id, sequence, cluster):
    span = max((math.dist(a['position'], b['position']) for a in cluster for b in cluster), default=0.0)
    return {
        'order_id': f'{run_id[:12]}-{sequence:02d}',
        'sheets': sorted({sheet for item in cluster for sheet in item['sheets']}),
        'centroid': [math.fsum(item['position'][0] for item in cluster) / len(cluster),
                     math.fsum(item['position'][1] for item in cluster) / len(cluster)],
        'span_m': span,
        'items': cluster,
    }


def plan_dispatch(snapshot, radius_m=DISPATCH_GROUP_RADIUS_M):
    candidates = outlier_candidates(snapshot['refit']) + seam_candidates(snapshot['seams'])
    positioned = [item for item in candidates if item['position'] is not None]
    stranded = [item for item in candidates if item['position'] is None]
    orders = [_order(snapshot['manifest']['run_id'], sequence, cluster)
              for sequence, cluster in enumerate(group_orders(positioned, radius_m), start=1)]
    return {'radius_m': radius_m, 'orders': orders,
            'undispatchable': sorted(stranded, key=lambda item: item['key'])}
