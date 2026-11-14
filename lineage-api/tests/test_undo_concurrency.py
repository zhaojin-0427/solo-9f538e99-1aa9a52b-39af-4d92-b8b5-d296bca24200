"""Undo vs. registration concurrency: readers see only complete states."""

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

BASE = {
    "nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"]]},
    ]
}
CHANGE = {
    "nodes": [
        {"name": "r1", "kind": "report", "candidate_groups": [["s2"]]},
        {"name": "extra", "kind": "derived", "candidate_groups": [["s1"]]},
    ]
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    db_file = tmp_path / "undo_concurrency.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(db_file))
    db.init_db(str(db_file))
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        pytest.fail("uvicorn server did not start")
    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


def test_concurrent_register_undo_seen_atomically(live_server):
    base_url = live_server
    r = httpx.post(f"{base_url}/lineage/register", json=BASE, timeout=10)
    assert r.status_code == 201

    errors: list[Exception] = []
    stop = threading.Event()

    def writer():
        # Register a change batch, then undo it; repeat forever.
        while not stop.is_set():
            try:
                r = httpx.post(
                    f"{base_url}/lineage/register", json=CHANGE, timeout=15
                )
                assert r.status_code == 201, r.text
                bid = r.json()["batch_id"]
                u = httpx.post(
                    f"{base_url}/lineage/batches/{bid}/undo", timeout=15
                )
                assert u.status_code == 200, u.text
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    def reader():
        while not stop.is_set():
            try:
                r = httpx.get(f"{base_url}/lineage/graph", timeout=15)
                assert r.status_code == 200
                nodes = {n["name"]: n for n in r.json()["nodes"]}
                groups = nodes["r1"]["candidate_groups"]
                if groups == [["s1"]]:
                    assert set(nodes) == {"s1", "s2", "r1"}, nodes.keys()
                elif groups == [["s2"]]:
                    assert set(nodes) == {"s1", "s2", "r1", "extra"}, nodes.keys()
                else:
                    raise AssertionError(f"partial undo state: {groups}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    def impact_reader():
        while not stop.is_set():
            try:
                r = httpx.post(
                    f"{base_url}/lineage/impact",
                    json={"retiring": ["s1"]},
                    timeout=15,
                )
                assert r.status_code == 200
                failed = {c["name"] for c in r.json()["failed_report_columns"]}
                assert failed in (set(), {"r1"})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, daemon=True),
        threading.Thread(target=reader, daemon=True),
        threading.Thread(target=impact_reader, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(4)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, [repr(e) for e in errors[:5]]
