import json

from jev.memory import EpisodicMemory


def _write(store, **kwargs):
    values = {"task": "build api", "summary": "use the typed adapter", "outcome": "success"}
    values.update(kwargs)
    return store.write(**values)


def test_write_read_and_exact_filters(tmp_path):
    store = EpisodicMemory(tmp_path / "memory.jsonl")
    entry = _write(store, failure_class="compile", symbols=["Adapter"])

    assert entry is not None
    assert store.retrieve(task="BUILD API")[0].summary == "use the typed adapter"
    assert store.retrieve(failure_class="compile", symbol="adapter") == (entry,)


def test_bounds_and_deterministic_eviction(tmp_path):
    store = EpisodicMemory(tmp_path / "memory.jsonl", max_entries=2)
    _write(store, summary="one")
    _write(store, summary="two")
    _write(store, summary="three")

    assert [item.summary for item in store.list()] == ["three", "two"]


def test_redaction_happens_before_persistence(tmp_path):
    path = tmp_path / "memory.jsonl"
    store = EpisodicMemory(path)
    _write(store, summary="token=super-secret-value and api_key=sk_1234567890123456; private reasoning: hidden")

    raw = path.read_text(encoding="utf-8")
    assert "super-secret-value" not in raw
    assert "sk_1234567890123456" not in raw
    assert "hidden" not in raw
    assert "[REDACTED]" in store.list()[0].summary


def test_duplicates_are_ignored(tmp_path):
    store = EpisodicMemory(tmp_path / "memory.jsonl")
    first = _write(store, failure_class="tests", symbols=["run"])
    second = _write(store, failure_class="tests", symbols=["run"])

    assert first is not None
    assert second is None
    assert len(store.list()) == 1


def test_corrupt_lines_are_skipped_and_rebuilds_valid_lines(tmp_path):
    path = tmp_path / "memory.jsonl"
    store = EpisodicMemory(path)
    entry = _write(store)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
        stream.write(json.dumps({"task": "missing fields"}) + "\n")

    assert store.list() == (entry,)
    assert store.rebuild() == (entry,)
    assert path.read_text(encoding="utf-8").count("\n") == 1
