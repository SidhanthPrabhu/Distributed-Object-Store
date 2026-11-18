from uuid import UUID

import pytest

from test_worker import DATA, config, later_put, put_headers, read_headers  # noqa: F401
from worker.corrupt import corrupt_chunk
from worker.store import ChunkStore


def test_fixed_scan_cursor_and_fault_survive_worker_restart(config):
    low, high = UUID(int=1), UUID(int=2)
    first = ChunkStore(config)
    header = put_headers()
    second_header = later_put(header, 2)
    try:
        first.put(low, header, DATA)
        first.put(high, second_header, DATA)
        first.scan_once()
        changed = corrupt_chunk(config, header.object_id, high)
        assert changed["corrupted_sha256"] != header.sha256
        assert first.integrity_batch() == ([], False)
    finally:
        first.close()

    second = ChunkStore(config)
    try:
        second.scan_once()
        reports, more = second.integrity_batch()
        assert not more and len(reports) == 1
        report = reports[0]
        assert report.chunk_id == high and report.outcome == "corrupt"
        assert second.integrity_batch()[0] == reports
        assert second.inventory().chunks[0].state == "valid"
        assert second.inventory().chunks[1].expected_sha256 == header.sha256
    finally:
        second.close()

    third = ChunkStore(config)
    try:
        assert third.integrity_batch()[0] == reports
        third.acknowledge_reports([report.report_id])
        third.scan_once()
        third.scan_once()
        assert third.integrity_batch() == ([], False)
        third.put(high, later_put(second_header, 3), DATA)
        assert third.get(high, read_headers(header)) == DATA
        assert third.get(low, read_headers(header)) == DATA
        with pytest.raises(ValueError):
            corrupt_chunk(config, UUID(int=3), low)
    finally:
        third.close()
