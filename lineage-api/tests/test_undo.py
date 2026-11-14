"""Batch undo: idempotency, conflict guards, and unrelated-field safety."""


def _register(client, nodes, expected=201):
    resp = client.post("/lineage/register", json={"nodes": nodes})
    assert resp.status_code == expected, resp.text
    return resp.json()


def _undo(client, batch_id, expected=200):
    resp = client.post(f"/lineage/batches/{batch_id}/undo")
    assert resp.status_code == expected, resp.text
    return resp.json()


def _graph(client):
    return {n["name"]: n for n in client.get("/lineage/graph").json()["nodes"]}


def test_register_returns_batch_id_and_undo_restores_definition(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"]]},
    ])
    batch = _register(client, [
        {"name": "r1", "kind": "report", "candidate_groups": [["s2"]]},
    ])
    assert isinstance(batch["batch_id"], int)
    assert _graph(client)["r1"]["candidate_groups"] == [["s2"]]

    result = _undo(client, batch["batch_id"])
    assert result["undone"] is True
    assert result["restored"] == ["r1"]
    assert result["deleted"] == []
    assert _graph(client)["r1"]["candidate_groups"] == [["s1"]]


def test_undo_deletes_fields_added_by_the_batch(client):
    _register(client, [{"name": "s1", "kind": "source"}])
    batch = _register(client, [
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
    ])
    result = _undo(client, batch["batch_id"])
    assert result["deleted"] == ["d1", "r1"]
    assert set(_graph(client)) == {"s1"}


def test_unrelated_later_batch_does_not_block_undo(client):
    bad = _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
    ])
    # A later batch only touches unrelated fields.
    _register(client, [
        {"name": "x1", "kind": "source"},
        {"name": "xr", "kind": "report", "candidate_groups": [["x1"]]},
    ])
    result = _undo(client, bad["batch_id"])
    assert result["deleted"] == ["d1", "s1"]
    assert set(_graph(client)) == {"x1", "xr"}


def test_later_redefinition_of_covered_field_rejects_undo(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"]]},
    ])
    first = _register(client, [
        {"name": "r1", "kind": "report", "candidate_groups": [["s2"]]},
    ])
    second = _register(client, [
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"], ["s2"]]},
    ])
    before = client.get("/lineage/graph").json()

    resp = client.post(f"/lineage/batches/{first['batch_id']}/undo")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "undo_conflict"
    assert body["details"]["modified_since_batch"] == ["r1"]
    # Rejected undo leaves the lineage untouched.
    assert client.get("/lineage/graph").json() == before

    # Undoing the latest batch restores the first batch's definition.
    _undo(client, second["batch_id"])
    assert _graph(client)["r1"]["candidate_groups"] == [["s2"]]
    _undo(client, first["batch_id"])
    assert _graph(client)["r1"]["candidate_groups"] == [["s1"]]


def test_later_batch_referencing_added_field_rejects_undo(client):
    _register(client, [{"name": "s1", "kind": "source"}])
    added = _register(client, [
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
    ])
    dependent = _register(client, [
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
    ])
    before = client.get("/lineage/graph").json()

    resp = client.post(f"/lineage/batches/{added['batch_id']}/undo")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "undo_conflict"
    assert body["details"]["referencing_nodes"] == ["r1"]
    assert client.get("/lineage/graph").json() == before

    # Removing the dependent reference unblocks the undo.
    _undo(client, dependent["batch_id"])
    assert _undo(client, added["batch_id"])["deleted"] == ["d1"]


def test_repeated_undo_replays_first_result(client):
    _register(client, [{"name": "s1", "kind": "source"}])
    batch = _register(client, [
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
    ])
    first = _undo(client, batch["batch_id"])
    second = _undo(client, batch["batch_id"])
    assert first == second
    assert set(_graph(client)) == {"s1"}


def test_failed_registration_leaves_no_undo_record(client):
    _register(client, [{"name": "s1", "kind": "source"}])
    before = client.get("/lineage/graph").json()
    resp = client.post("/lineage/register", json={"nodes": [
        {"name": "c1", "kind": "derived", "candidate_groups": [["c2"]]},
        {"name": "c2", "kind": "derived", "candidate_groups": [["c1"]]},
    ]})
    assert resp.status_code == 409
    assert client.get("/lineage/graph").json() == before
    # The failed batch consumed no batch id: the next success is contiguous.
    ok = _register(client, [{"name": "s2", "kind": "source"}])
    assert ok["batch_id"] == 2
    # Both existing ids are undoable; the failed batch has no record at all.
    assert _undo(client, 1)["undone"] is True
    assert _undo(client, ok["batch_id"])["undone"] is True


def test_unknown_batch_returns_404(client):
    resp = client.post("/lineage/batches/987654/undo")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_undo_restores_original_and_deletes_only_own_additions(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "old", "kind": "derived", "candidate_groups": [["s1"]]},
    ])
    batch = _register(client, [
        {"name": "old", "kind": "derived", "candidate_groups": [["s1"], ["s3"]]},
        {"name": "s3", "kind": "source"},
    ])
    result = _undo(client, batch["batch_id"])
    assert result["restored"] == ["old"]
    assert result["deleted"] == ["s3"]
    assert _graph(client)["old"]["candidate_groups"] == [["s1"]]


def test_undo_when_existing_field_adopts_a_batch_added_field(client):
    """A covered field that now depends on a batch-added field is restored first."""
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report", "candidate_groups": [["s1"]]},
    ])
    batch = _register(client, [
        {"name": "d1", "kind": "derived", "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report", "candidate_groups": [["d1"]]},
    ])
    result = _undo(client, batch["batch_id"])
    assert result["restored"] == ["r1"]
    assert result["deleted"] == ["d1"]
    assert _graph(client)["r1"]["candidate_groups"] == [["s1"]]

