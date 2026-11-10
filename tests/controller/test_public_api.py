"""A4 public lifecycle checks with contract doubles, not real worker evidence."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import pytest

from common.config import WORKER_IDS
from common.errors import APIError
from common.models import ObjectMetadata
from test_object_path import WorkerDouble, put, register_all, running


class PublicWorkers(WorkerDouble):
    cleanup_reply = None
    observe_cleanup = None

    def __call__(self, request):
        result = super().__call__(request)
        if request.url.path.endswith('/tombstone'):
            if self.observe_cleanup:
                self.observe_cleanup(request)
            if self.cleanup_reply:
                return self.cleanup_reply(request, result)
        return result


@pytest.fixture
def workers(identities):
    return PublicWorkers(identities)


def error(response, status, code):
    assert response.status_code == status, response.text
    assert set(response.json()) == {'error'}
    detail = response.json()['error']
    assert set(detail) == {'code', 'message', 'request_id'}
    assert detail['code'] == code and detail['message']
    assert str(UUID(detail['request_id'])) == response.headers['X-Request-ID']


def finish_deletion(client, app, oid):
    # Maintenance may finish between restoring the worker and sending a retry.
    # A new request after completion must return 404 under the public contract.
    response = client.delete(f'/api/v1/objects/{oid}')
    assert response.status_code in {204, 404}, response.text
    assert app.state.store.object_state(UUID(str(oid))) == 'DELETED'
    if response.status_code == 204:
        assert response.content == b''


@pytest.mark.parametrize('data', [b'', b'\x00\xffbinary' * 3])
def test_complete_metadata_patch_is_local_and_content_immutable(tmp_path, workers, data):
    with running(tmp_path, workers, chunk_size_bytes=4) as (client, app, _):
        register_all(client, app, workers)
        original = put(client, data, {'keep': [1, True, None], 'nested': {'old': 1}}).json()
        url = f"/api/v1/objects/{original['object_id']}"
        before = client.get(url + '/metadata')
        assert before.status_code == 200 and before.json() == original
        ObjectMetadata.model_validate(before.json())
        assert original['checksum'] == {'algorithm': 'sha256', 'value': sha256(data).hexdigest()}
        assert [c['ordinal'] for c in original['chunks']] == list(range(original['chunk_count']))
        assert all(len(c['locations']) == len(set(c['locations'])) == 2 for c in original['chunks'])
        calls = list(workers.calls)
        # Even system-looking names are custom keys under the agreed direct-map shape.
        changes = {'nested': {'new': [False, {'float': 1.25}]}, 'null': None,
                   'checksum': 'custom only', 'size_bytes': 999, 'generation': 'custom'}
        response = client.patch(url + '/metadata', json=changes)
        assert response.status_code == 200
        updated = response.json()
        assert updated['custom_metadata'] == {**original['custom_metadata'], **changes}
        assert updated['metadata_revision'] == 2
        assert updated['modified_at'] > original['modified_at']
        for key in original.keys() - {'custom_metadata', 'metadata_revision', 'modified_at'}:
            assert updated[key] == original[key]
        assert workers.calls == calls  # No chunk PUTs, reads, or cleanup during PATCH.
        assert client.get(url).content == data
        empty_patch = client.patch(url + '/metadata', json={}).json()
        assert empty_patch['metadata_revision'] == 3
        assert empty_patch['custom_metadata'] == updated['custom_metadata']


def test_concurrent_patch_merges_do_not_lose_keys_or_revisions(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        obj = put(client).json()
        url = f"/api/v1/objects/{obj['object_id']}/metadata"
        def patch(i):
            response = client.patch(url, json={str(i): {'value': i}})
            assert response.status_code == 200
            return response.json()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(patch, range(24)))
        assert sorted(r['metadata_revision'] for r in results) == list(range(2, 26))
        final = client.get(url).json()
        assert final['custom_metadata'] == {str(i): {'value': i} for i in range(24)}
        assert final['metadata_revision'] == 25
        assert len(workers.puts) == 2


@pytest.mark.parametrize('raw', ['[]', 'null', 'false', '5', '"text"', '{broken', '',
                               '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}',
                               '{"x":"' + 'x' * 65536 + '"}', '{"x":' + '[' * 1100 + '0' + ']' * 1100 + '}'])
def test_invalid_patch_is_400_and_transaction_does_not_change(tmp_path, workers, raw):
    with running(tmp_path, workers) as (client, app, _):
        obj = put(client, b'').json()
        url = f"/api/v1/objects/{obj['object_id']}/metadata"
        response = client.patch(url, content=raw, headers={'Content-Type': 'application/json'})
        error(response, 400, 'invalid_request')
        assert client.get(url).json() == obj


def test_patch_requires_json_and_public_id_errors_are_consistent(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        for identity, status in [('not-a-uuid', 400), (str(uuid4()), 404)]:
            url = '/api/v1/objects/' + identity
            code = 'invalid_request' if status == 400 else 'object_not_found'
            for response in [client.get(url), client.get(url + '/metadata'),
                             client.patch(url + '/metadata', json={}), client.delete(url)]:
                error(response, status, code)
        obj = put(client, b'').json()
        error(client.patch(f"/api/v1/objects/{obj['object_id']}/metadata", content='{}'),
              400, 'invalid_request')
        for kwargs in [dict(content='not multipart'), dict(files={'other': ('x', b'x')}),
                       dict(files=[('file', ('x', b'x')), ('file', ('y', b'y'))]),
                       dict(content=b'broken', headers={'Content-Type': 'multipart/form-data'}),
                       dict(content=b'bad', headers={'Content-Type': 'multipart/form-data; boundary=BOUND'})]:
            error(client.post('/api/v1/objects', **kwargs), 400, 'invalid_request')
        assert client.get('/api/v1/objects').status_code == 200


def test_list_and_all_routes_hide_incomplete_deleting_deleted(tmp_path, workers, manifest_factory):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        active = put(client, b'').json()
        hidden = manifest_factory()
        app.state.store.create_upload(hidden)
        workers.offline.add('worker4')
        deleting = put(client).json()
        url = f"/api/v1/objects/{deleting['object_id']}"
        error(client.delete(url), 503, 'deletion_pending')
        for oid in [hidden.object_id, deleting['object_id']]:
            base = f'/api/v1/objects/{oid}'
            error(client.get(base), 404, 'object_not_found')
            error(client.get(base + '/metadata'), 404, 'object_not_found')
            error(client.patch(base + '/metadata', json={'x': 1}), 404, 'object_not_found')
        error(client.delete(f'/api/v1/objects/{hidden.object_id}'), 404, 'object_not_found')
        summary = {k: active[k] for k in ['object_id', 'name', 'size_bytes', 'content_type']}
        assert client.get('/api/v1/objects').json() == {'objects': [summary]}
        workers.offline.clear()
        finish_deletion(client, app, deleting['object_id'])
        error(client.delete(url), 404, 'object_not_found')
        assert client.get('/api/v1/objects').json() == {'objects': [summary]}


@pytest.mark.parametrize('data', [b'', b'abcdefgh'])
def test_delete_records_durable_intent_before_all_four_worker_effects(tmp_path, workers, data):
    with running(tmp_path, workers, chunk_size_bytes=4) as (client, app, _):
        register_all(client, app, workers)
        obj = put(client, data).json()
        oid = UUID(obj['object_id'])
        observed = []
        def check(request):
            assert not app.state.store._db.in_transaction
            assert app.state.store.object_state(oid) == 'DELETING'
            assert app.state.store.pending_cleanup()
            assert app.state.store.list_objects() == []
            observed.append(request.url.host)
        workers.observe_cleanup = check
        response = client.delete(f'/api/v1/objects/{oid}')
        assert response.status_code == 204 and response.content == b''
        assert set(observed) == set(WORKER_IDS)
        assert all(str(oid) in t for t in workers.tombstones.values())
        assert all(not chunks for chunks in workers.chunks.values())
        assert app.state.store.pending_operations() == app.state.store.pending_cleanup() == []


@pytest.mark.parametrize('bad', [
    {'cleanup_complete': False}, {'cleanup_complete': 1}, {'boot_id': str(uuid4())},
    {'storage_id': str(uuid4())}, {'worker_id': 'worker4'}, {'generation': str(uuid4())},
    {'object_id': str(uuid4())}, {'operation_id': str(uuid4())},
    {'operation_sequence': 987654}, {'controller_epoch': 999},
])
def test_invalid_cleanup_receipts_cannot_complete_public_delete(tmp_path, workers, bad):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        oid = put(client).json()['object_id']
        def corrupt(request, response):
            if request.url.host == 'worker1':
                return httpx.Response(200, json={**response.json(), **bad})
            return response
        workers.cleanup_reply = corrupt
        error(client.delete(f'/api/v1/objects/{oid}'), 503, 'deletion_pending')
        assert any(n['worker_id'] == 'worker1' for n in app.state.store.pending_cleanup())
        assert app.state.store.object_state(UUID(oid)) == 'DELETING'
        workers.cleanup_reply = None
        finish_deletion(client, app, oid)


def test_lost_cleanup_acknowledgements_retry_same_intent(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        oid = put(client).json()['object_id']
        seen = []
        def lose(request, response):
            if request.url.host == 'worker1':
                seen.append(json.loads(request.content))
                raise httpx.ReadTimeout('lost after cleanup', request=request)
            return response
        workers.cleanup_reply = lose
        error(client.delete(f'/api/v1/objects/{oid}'), 503, 'deletion_pending')
        assert len(seen) >= 2 and all(body == seen[0] for body in seen)
        assert not workers.chunks['worker1']
        workers.cleanup_reply = None
        finish_deletion(client, app, oid)
        assert workers.tombstones['worker1'][oid] == seen[0]


def test_restart_during_cleanup_reissues_pending_intent_and_preserves_other_objects(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        retained = put(client, metadata={'keep': True}).json()
        obj = put(client).json()
        oid = UUID(obj['object_id'])
        def interrupt(request, response):
            if request.url.host == 'worker2':
                raise asyncio.CancelledError()  # remote effect, no acknowledgement
            return response
        workers.cleanup_reply = interrupt
        async def interrupted():
            with pytest.raises(asyncio.CancelledError):
                await app.state.transfers.delete(oid)
        client.portal.call(interrupted)
        old = app.state.store.cleanup_intent(oid)
        assert app.state.store.object_state(oid) == 'DELETING'
        assert any(n['worker_id'] == 'worker2' for n in app.state.store.pending_cleanup())
    workers.cleanup_reply = None
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        assert app.state.store.object_state(oid) == 'DELETED'
        replay = workers.tombstones['worker2'][str(oid)]
        assert replay['controller_epoch'] > old['controller_epoch']
        assert replay['operation_sequence'] > old['operation_sequence']
        assert replay['operation_id'] != old['operation_id']
        assert client.get(f"/api/v1/objects/{retained['object_id']}/metadata").json() == retained
        assert client.get(f"/api/v1/objects/{retained['object_id']}").content == b'hello'
        error(client.delete(f'/api/v1/objects/{oid}'), 404, 'object_not_found')


def test_patch_delete_races_never_update_a_hidden_object(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        workers.offline.add('worker4')
        for _ in range(8):
            oid = put(client, b'').json()['object_id']
            barrier = Barrier(2)
            def patch():
                barrier.wait()
                return client.patch(f'/api/v1/objects/{oid}/metadata', json={'raced': True})
            def delete():
                barrier.wait()
                return client.delete(f'/api/v1/objects/{oid}')
            with ThreadPoolExecutor(max_workers=2) as pool:
                pf, df = pool.submit(patch), pool.submit(delete)
                patched, deleted = pf.result(), df.result()
            assert patched.status_code in {200, 404}
            error(deleted, 503, 'deletion_pending')
            hidden = app.state.store.get_manifest(UUID(oid), include_hidden=True)
            assert hidden.metadata_revision == (2 if patched.status_code == 200 else 1)
            error(client.patch(f'/api/v1/objects/{oid}/metadata', json={'later': 1}), 404, 'object_not_found')
        assert client.get('/api/v1/objects').json() == {'objects': []}


def test_delayed_put_receipt_and_write_cannot_revive_publicly_deleted_object(tmp_path, workers):
    from common.models import ChunkPutHeaders, ChunkReceipt
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        obj = put(client).json()
        oid = UUID(obj['object_id'])
        chunk = obj['chunks'][0]
        op = app.state.store.create_put_intent(oid, UUID(chunk['chunk_id']), 'worker3')
        headers = ChunkPutHeaders(**{k: op[k] for k in ChunkPutHeaders.model_fields})
        request = httpx.Request('PUT', f"http://worker3:8000/internal/chunks/{chunk['chunk_id']}",
                               headers=headers.to_headers(), content=b'hello')
        delayed_receipt = ChunkReceipt.model_validate(workers(request).json())
        assert client.delete(f'/api/v1/objects/{oid}').status_code == 204
        with pytest.raises(APIError) as caught:
            app.state.store.record_receipt(delayed_receipt)
        assert caught.value.code == 'stale_operation'
        assert workers(request).status_code == 409
        with pytest.raises(APIError):
            app.state.store.create_put_intent(oid, UUID(chunk['chunk_id']), 'worker1')
        with pytest.raises(APIError):
            app.state.store.publish_upload(oid)
        assert app.state.store.list_objects() == []
        assert all(not chunks for chunks in workers.chunks.values())


@pytest.mark.parametrize('offline', ['worker1', 'worker4'])
def test_offline_worker_does_not_prevent_other_cleanup_and_retry(tmp_path, workers, offline):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        oid = put(client).json()['object_id']
        workers.offline.add(offline)
        error(client.delete(f'/api/v1/objects/{oid}'), 503, 'deletion_pending')
        assert app.state.store.pending_cleanup() == [
            {'object_id': oid, 'worker_id': offline, 'acknowledged': 0}]
        assert all(oid in workers.tombstones[w] for w in WORKER_IDS if w != offline)
        op = app.state.store.cleanup_intent(UUID(oid))
        error(client.delete(f'/api/v1/objects/{oid}'), 503, 'deletion_pending')
        assert app.state.store.cleanup_intent(UUID(oid)) == op
        workers.offline.clear()
        finish_deletion(client, app, oid)


def test_deletion_hides_before_waiting_for_transfer_lock(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        oid = UUID(put(client).json()['object_id'])
        async def ordered():
            transfers = app.state.transfers
            await transfers.lock.acquire()
            task = asyncio.create_task(transfers.delete(oid))
            try:
                await asyncio.sleep(0)
                assert not task.done()
                assert app.state.store.object_state(oid) == 'DELETING'
                assert app.state.store.pending_cleanup()
                with pytest.raises(APIError) as caught:
                    app.state.store.patch_metadata(oid, {'too_late': True})
                assert caught.value.status_code == 404
                assert app.state.store.list_objects() == []
                assert all(str(oid) not in t for t in workers.tombstones.values())
            finally:
                transfers.lock.release()
                await task
        client.portal.call(ordered)
        error(client.get(f'/api/v1/objects/{oid}/metadata'), 404, 'object_not_found')


@pytest.mark.parametrize('response', [httpx.Response(409), httpx.Response(503),
                                     httpx.Response(200, content=b'not JSON')])
def test_worker_errors_are_pending_deletion_not_public_conflicts(tmp_path, workers, response):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        oid = put(client).json()['object_id']
        workers.cleanup_reply = lambda request, original: response if request.url.host == 'worker1' else original
        error(client.delete(f'/api/v1/objects/{oid}'), 503, 'deletion_pending')
        assert app.state.store.object_state(UUID(oid)) == 'DELETING'


def test_new_worker_boot_during_cleanup_rejects_old_receipt(tmp_path, workers):
    from common.models import WorkerRegistration
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        oid = put(client).json()['object_id']
        def reboot(request, response):
            if request.url.host == 'worker1':
                new_identity = {**workers.identities['worker1'], 'boot_id': str(uuid4())}
                workers.identities['worker1'] = new_identity
                app.state.registry.register(WorkerRegistration(**new_identity))
            return response
        workers.cleanup_reply = reboot
        error(client.delete(f'/api/v1/objects/{oid}'), 503, 'deletion_pending')
        assert any(n['worker_id'] == 'worker1' for n in app.state.store.pending_cleanup())
        workers.cleanup_reply = None
        finish_deletion(client, app, oid)


def test_revision_conflict_is_409_without_partial_update(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        obj = put(client, b'').json()
        oid = UUID(obj['object_id'])
        with app.state.store.transaction() as db:
            db.execute('UPDATE objects SET metadata_revision=? WHERE object_id=?', (2**63 - 1, str(oid)))
        before = client.get(f'/api/v1/objects/{oid}/metadata').json()
        error(client.patch(f'/api/v1/objects/{oid}/metadata', json={'x': True}),
              409, 'metadata_revision_exhausted')
        assert client.get(f'/api/v1/objects/{oid}/metadata').json() == before


@pytest.mark.parametrize('method,path,component,attribute', [
    ('GET', '/api/v1/objects', 'store', 'list_objects'),
    ('GET', '/api/v1/objects/{id}/metadata', 'store', 'get_manifest'),
    ('PATCH', '/api/v1/objects/{id}/metadata', 'store', 'patch_metadata'),
    ('POST', '/api/v1/objects', 'transfers', 'upload'),
    ('GET', '/api/v1/objects/{id}', 'transfers', 'download'),
    ('DELETE', '/api/v1/objects/{id}', 'transfers', 'delete'),
])
def test_unexpected_public_failures_have_sanitized_envelope(tmp_path, workers, monkeypatch,
                                                          method, path, component, attribute):
    with running(tmp_path, workers) as (client, app, _):
        def broken(*args, **kwargs):
            raise RuntimeError('private storage path /do/not/expose')
        monkeypatch.setattr(getattr(app.state, component), attribute, broken)
        kwargs = {'json': {}} if method == 'PATCH' else {}
        if method == 'POST':
            kwargs = {'files': {'file': ('x', b'')}}
        response = client.request(method, path.format(id=uuid4()), **kwargs)
        error(response, 500, 'internal_error')
        assert 'private' not in response.text and '/do/not/expose' not in response.text
