"""Manual real-service A4 acceptance probe. Run from the repository root.

Uses the existing Compose deployment and disposable public objects. Restarts the
controller and stops/starts worker4; never deletes volumes or unrelated objects.
Not collected by pytest. Requires Python's standard library and Docker Compose.
"""

import base64
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = 'http://127.0.0.1:8000'
WORKERS = [f'worker{i}' for i in range(1, 5)]


def compose(*args, input=None):
    return subprocess.check_output(['docker', 'compose', *args], input=input, text=True)


def inside(service, source, payload=None):
    return json.loads(compose('exec', '-T', service, 'python', '-c', source,
                              input=json.dumps(payload)))


def call(method, path, body=None, content_type=None):
    headers = {'Content-Type': content_type} if content_type else {}
    if isinstance(body, dict):
        body, headers = json.dumps(body).encode(), {'Content-Type': 'application/json'}
    try:
        response = urlopen(Request(BASE + path, data=body, method=method, headers=headers), timeout=40)
    except HTTPError as exc:
        response = exc
    data = response.read()
    if response.headers.get_content_type() == 'application/json':
        data = json.loads(data)
    if response.status >= 400:
        assert data['error']['request_id'] == response.headers['X-Request-ID']
    return response.status, data


def wait_for(check, label):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (URLError, ConnectionError):
            pass
        time.sleep(0.25)
    raise AssertionError('Timed out: ' + label)


WORKER_HTTP = '''
import base64, json, sys, urllib.request, urllib.error
p=json.load(sys.stdin)
r=urllib.request.Request('http://' + p['worker'] + ':8000' + p['path'],
    data=base64.b64decode(p['body']) if 'body' in p else None,
    headers=p.get('headers', {}), method=p.get('method', 'GET'))
try:
    response=urllib.request.urlopen(r, timeout=5)
except urllib.error.HTTPError as e:
    response=e
body=response.read()
print(json.dumps({'status':response.status, 'data':json.loads(body) if body else None}))
'''


def worker_http(worker, path, **kwargs):
    return inside('controller', WORKER_HTTP, {'worker': worker, 'path': path, **kwargs})


def ready():
    return call('GET', '/health/ready')[0] == 200 and all(
        worker_http(w, '/health/ready')['status'] == 200 for w in WORKERS)


WORKER_SNAPSHOT = '''
import json, sqlite3, sys
from pathlib import Path
p=json.load(sys.stdin)
db=sqlite3.connect('file:/data/catalog.sqlite3?mode=ro', uri=True)
db.row_factory=sqlite3.Row
ids=p['chunks']
ops=[dict(r) for r in db.execute('SELECT * FROM operations ORDER BY operation_sequence')
     if r['chunk_id'] in ids]
t=[dict(r) for r in db.execute('SELECT * FROM object_tombstones WHERE object_id=?', (p['object_id'],))]
placements=[dict(r) for r in db.execute('SELECT * FROM placements WHERE object_id=?', (p['object_id'],))]
files=sorted(str(f.relative_to('/data')) for f in Path('/data/chunks').rglob('*')
             if f.is_file() and any(cid in f.name for cid in ids))
print(json.dumps({'operations':ops,'tombstones':t,'placements':placements,'files':files}))
'''


def snapshot(obj):
    return {w: inside(w, WORKER_SNAPSHOT, {'object_id': obj['object_id'],
                                         'chunks': [c['chunk_id'] for c in obj['chunks']]}) for w in WORKERS}


def upload(data):
    boundary = 'a4' + uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
            '{"keep":{"values":[true,null,1]},"version":0}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a4-disposable.bin"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    status, obj = call('POST', '/api/v1/objects', body, 'multipart/form-data; boundary=' + boundary)
    assert status == 201, (status, obj)
    return obj


def assert_hidden(oid):
    for method, suffix, body in [('GET', '', None), ('GET', '/metadata', None),
                                 ('PATCH', '/metadata', {'later': True})]:
        status, result = call(method, '/api/v1/objects/' + oid + suffix, body)
        assert status == 404 and result['error']['code'] == 'object_not_found'
    assert oid not in [o['object_id'] for o in call('GET', '/api/v1/objects')[1]['objects']]


def main():
    created = []
    result = {}
    try:
        wait_for(ready, 'all five services ready')
        initial = call('GET', '/api/v1/objects')[1]['objects']
        data = bytes(range(256)) * 7813  # 2,000,128 bytes, three chunks
        obj = upload(data)
        created.append(obj['object_id'])
        url = '/api/v1/objects/' + obj['object_id']
        assert [c['size_bytes'] for c in obj['chunks']] == [1000000, 1000000, 128]
        assert call('GET', url + '/metadata') == (200, obj)
        copies = snapshot(obj)
        assert sum(len(s['placements']) for s in copies.values()) == 6
        updated = call('PATCH', url + '/metadata', {'version': 1, 'new': [False, {'x': 1.25}],
                                                   'checksum': 'custom only'})
        assert updated[0] == 200
        patched = updated[1]
        assert patched['custom_metadata']['keep'] == obj['custom_metadata']['keep']
        assert patched['custom_metadata']['version'] == 1
        assert patched['metadata_revision'] == 2 and patched['modified_at'] > obj['modified_at']
        for key in obj.keys() - {'custom_metadata', 'metadata_revision', 'modified_at'}:
            assert obj[key] == patched[key]
        with ThreadPoolExecutor(max_workers=6) as pool:
            revisions = list(pool.map(lambda i: call('PATCH', url + '/metadata', {str(i): i}), range(12)))
        assert all(s == 200 for s, _ in revisions)
        assert sorted(m['metadata_revision'] for _, m in revisions) == list(range(3, 15))
        final = call('GET', url + '/metadata')[1]
        assert all(final['custom_metadata'][str(i)] == i for i in range(12))
        assert snapshot(obj) == copies  # no worker writes or data/file changes on PATCH
        assert call('GET', url) == (200, data)
        assert obj['object_id'] in [o['object_id'] for o in call('GET', '/api/v1/objects')[1]['objects']]
        for method, path, body in [('GET', '/api/v1/objects/bad/metadata', None),
                                   ('DELETE', '/api/v1/objects/bad', None),
                                   ('PATCH', url + '/metadata', b'[]'),
                                   ('POST', '/api/v1/objects', b'bad')]:
            assert call(method, path, body, 'application/json')[0] == 400
        empty = upload(b'')
        created.append(empty['object_id'])
        assert empty['chunks'] == [] and empty['checksum']['value'] == sha256(b'').hexdigest()
        assert call('PATCH', '/api/v1/objects/' + empty['object_id'] + '/metadata', {'empty': True})[0] == 200
        assert call('GET', '/api/v1/objects/' + empty['object_id']) == (200, b'')
        assert call('DELETE', '/api/v1/objects/' + empty['object_id']) == (204, b'')
        assert_hidden(empty['object_id'])
        assert call('DELETE', '/api/v1/objects/' + empty['object_id'])[0] == 404
        assert all(s['tombstones'][0]['cleanup_complete'] == 1 for s in snapshot(empty).values())
        result['metadata_patch_list_empty_errors'] = 'passed'
        result['patch_chunk_writes'] = 0
        result['size_bytes'], result['sha256'] = len(data), sha256(data).hexdigest()
        result['object_id'] = obj['object_id']
        # Keep the original authorized PUT identity for a delayed replay after deletion.
        first_chunk = obj['chunks'][0]
        location = first_chunk['locations'][0]
        op = next(o for o in copies[location]['operations'] if o['chunk_id'] == first_chunk['chunk_id'])
        compose('stop', 'worker4')
        status, pending = call('DELETE', url)
        assert status == 503 and pending['error']['code'] == 'deletion_pending'
        assert_hidden(obj['object_id'])
        status, pending = call('DELETE', url)
        assert status == 503 and pending['error']['code'] == 'deletion_pending'
        survivor = upload(b'A4 survivor bytes')
        created.append(survivor['object_id'])
        survivor_url = '/api/v1/objects/' + survivor['object_id']
        survivor = call('PATCH', survivor_url + '/metadata', {'survives': {'restart': True}})[1]
        before_restart = call('GET', '/health/ready')[1]['controller_epoch']
        # Simulate a worker effect whose acknowledgement cannot reach the controller.
        # Read the existing pending intent; never write a runtime database directly.
        intent = inside('controller', """
import json, sqlite3, sys
p=json.load(sys.stdin)
db=sqlite3.connect('file:/data/metadata.sqlite3?mode=ro', uri=True)
db.row_factory=sqlite3.Row
r=db.execute('SELECT operations.* FROM tombstones JOIN operations USING(operation_id) WHERE tombstones.object_id=?', (p,)).fetchone()
print(json.dumps(dict(r)))
""", obj['object_id'])
        compose('stop', 'controller')
        subprocess.check_output(['docker', 'start', 'worker4'], text=True)
        inside('worker4', r"""
import json, socket, sys, time
p=json.load(sys.stdin)
body=json.dumps({k:p[k] for k in ('generation','operation_id','controller_epoch','operation_sequence')}).encode()
header=('POST /internal/objects/' + p['object_id'] + '/tombstone HTTP/1.1\r\n'
        'Host: localhost\r\nContent-Type: application/json\r\nContent-Length: '
        + str(len(body)) + '\r\nConnection: close\r\n\r\n').encode()
deadline=time.monotonic()+15
while True:
    try:
        sock=socket.create_connection(('127.0.0.1',8000),timeout=5)
        break
    except ConnectionRefusedError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.1)
with sock:
    sock.sendall(header + body)
    sock.shutdown(socket.SHUT_WR)
    # Deliberately discard the response. No controller acknowledgement is recorded.
print(json.dumps({'sent':True}))
""", intent)
        def lost_effect_done():
            tombstones = inside('worker4', WORKER_SNAPSHOT, {'object_id': obj['object_id'],
                    'chunks': [c['chunk_id'] for c in obj['chunks']]})['tombstones']
            return bool(tombstones) and tombstones[0]['cleanup_complete'] == 1
        wait_for(lost_effect_done, 'worker4 applies cleanup without controller acknowledgement')
        compose('start', 'controller')
        wait_for(lambda: call('GET', '/health/ready')[0] == 200, 'controller restart readiness')
        after_restart = call('GET', '/health/ready')[1]['controller_epoch']
        assert after_restart > before_restart
        assert_hidden(obj['object_id'])
        wait_for(ready, 'worker4 rejoins and cleanup completes')
        assert call('GET', survivor_url + '/metadata') == (200, survivor)
        assert call('GET', survivor_url) == (200, b'A4 survivor bytes')
        assert call('DELETE', survivor_url) == (204, b'')
        assert call('DELETE', url)[0] == 404  # maintenance finished the pending deletion
        completed = snapshot(obj)
        assert all(s['tombstones'][0]['cleanup_complete'] == 1
                   and all(p['state'] == 'retired' for p in s['placements'])
                   and not s['files'] for s in completed.values())
        assert completed['worker4']['tombstones'][0]['controller_epoch'] == after_restart
        headers = {'X-Object-ID': obj['object_id'], 'X-Object-Generation': obj['generation'],
                   'X-Operation-ID': op['operation_id'], 'X-Controller-Epoch': str(op['controller_epoch']),
                   'X-Operation-Sequence': str(op['operation_sequence']),
                   'X-Chunk-SHA256': first_chunk['sha256'], 'X-Chunk-Length': str(first_chunk['size_bytes']),
                   'Content-Type': 'application/octet-stream'}
        replay = worker_http(location, '/internal/chunks/' + first_chunk['chunk_id'], method='PUT',
                             headers=headers, body=base64.b64encode(data[:1000000]).decode())
        assert replay['status'] == 409
        assert_hidden(obj['object_id'])
        assert call('GET', '/api/v1/objects')[1]['objects'] == initial
        result.update(offline_deletion_pending='passed', pending_retry='passed',
                      restart_during_pending_cleanup='passed', lost_cleanup_acknowledgement='passed',
                      patched_survivor_after_restart='passed', all_four_durable_tombstones='passed',
                      delayed_put_status=replay['status'], controller_epochs=[before_restart, after_restart],
                      existing_objects_preserved=len(initial))
        print(json.dumps(result, indent=2))
    finally:
        # Restore service availability on assertion failure; remove only our IDs.
        compose('start', 'controller', 'worker4')
        wait_for(lambda: call('GET', '/health/ready')[0] == 200, 'cleanup availability')
        for oid in created:
            try:
                status, _ = call('DELETE', '/api/v1/objects/' + oid)
                assert status in {204, 404, 503}
            except (URLError, ConnectionError):
                pass


if __name__ == '__main__':
    main()
