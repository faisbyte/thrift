import json

from reprouter.corpus import hash_message, load_jsonl_session, reconstruct_session


def H(m):
    return hash_message(m)


SYSTEM = [{"type": "text", "text": "billing"}, {"type": "text", "text": "You are Claude Code."},
          {"type": "text", "text": "cwd: /w"}]
TOOLS = [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}]
U1 = {"role": "user", "content": [{"type": "text", "text": "fix it"}]}
SYS_MSG = {"role": "system", "content": [{"type": "text", "text": "reminder"}]}
A1_BLOCKS = [{"type": "thinking", "thinking": "", "signature": "s"},
             {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/w/a.py"}}]
A1 = {"role": "assistant", "content": A1_BLOCKS}
U2 = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1\tx = 1\n"}]}


def records():
    shape_hash = "shape1"
    return [
        {"type": "api-request-shape", "sessionId": "s1", "shapeHash": shape_hash,
         "shape": {"system": SYSTEM, "tools": TOOLS}},
        {"type": "api-request-blob", "sessionId": "s1", "hash": H(U1), "message": U1},
        {"type": "api-request-blob", "sessionId": "s1", "hash": H(SYS_MSG), "message": SYS_MSG},
        {"type": "api-request", "sessionId": "s1", "id": "r1", "timestamp": "2026-10-20T10:00:00.000Z",
         "querySource": "sdk", "shapeHash": shape_hash, "keep": 0, "tail": [H(U1), H(SYS_MSG)],
         "params": {"model": "claude-haiku-5-5", "max_tokens": 32000, "thinking": {"type": "adaptive"}},
         "cacheControl": [{"path": ["system", 1], "value": {"type": "ephemeral", "ttl": "1h"}},
                          {"path": ["messages", 1, "content", 0], "value": {"type": "ephemeral", "ttl": "1h"}}]},
        # the assistant reply is stored as per-block transcript records sharing a message id
        {"type": "assistant", "sessionId": "s1", "message": {"id": "m1", "role": "assistant", "content": [A1_BLOCKS[0]]}},
        {"type": "assistant", "sessionId": "s1", "message": {"id": "m1", "role": "assistant", "content": [A1_BLOCKS[1]]}},
        {"type": "user", "sessionId": "s1", "message": U2},
        {"type": "api-request", "sessionId": "s1", "id": "r2", "timestamp": "2026-10-20T10:00:05.000Z",
         "querySource": "sdk", "shapeHash": shape_hash, "keep": 2, "tail": [H(A1), H(U2)],
         "params": {"model": "claude-haiku-5-5", "max_tokens": 32000},
         "cacheControl": [{"path": ["system", 1], "value": {"type": "ephemeral", "ttl": "1h"}},
                          {"path": ["messages", 3, "content", 0], "value": {"type": "ephemeral", "ttl": "1h"}}]},
    ]


def test_hash_matches_claude_code_canonical_json():
    import hashlib

    m = {"role": "user", "content": [{"type": "text", "text": "é"}]}
    canon = json.dumps(m, sort_keys=True, separators=(",", ":"))
    assert hash_message(m) == hashlib.sha256(canon.encode()).hexdigest()


def test_first_request_from_blobs_and_shape():
    reqs = reconstruct_session(records())
    r1 = reqs[0]
    assert r1.complete and r1.request_id == "r1" and r1.model == "claude-haiku-5-5"
    assert r1.body["tools"] == TOOLS
    assert [m["role"] for m in r1.body["messages"]] == ["user", "system"]
    assert r1.body["system"][1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in r1.body["system"][0]
    assert r1.body["messages"][1]["content"][0]["cache_control"]["ttl"] == "1h"


def test_second_request_keeps_prefix_and_resolves_assistant_from_transcript():
    r2 = reconstruct_session(records())[1]
    assert r2.complete
    msgs = r2.body["messages"]
    assert [m["role"] for m in msgs] == ["user", "system", "assistant", "user"]
    assert msgs[2]["content"][1]["name"] == "Read"
    assert msgs[3]["content"][0]["content"] == "1\tx = 1\n"
    # cache markers from request 1 must not leak into request 2
    assert "cache_control" not in msgs[1]["content"][0]
    assert msgs[3]["content"][0]["cache_control"]["ttl"] == "1h"


def test_unresolvable_tail_marks_request_and_successors_incomplete():
    recs = records()
    recs = [r for r in recs if r.get("type") != "user"]  # drop the tool_result record
    reqs = reconstruct_session(recs)
    assert reqs[0].complete
    assert not reqs[1].complete and reqs[1].missing == 1


def test_unresolved_tool_result_message_is_approximated_from_matching_records():
    recs = records()
    # the API form of this tool_result differs from the stored one, so its hash won't match
    for r in recs:
        if r.get("type") == "user":
            r["message"] = {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "1\tx = 1\n", "is_error": False}]}
    r2 = reconstruct_session(recs)[1]
    assert not r2.complete and r2.approx and r2.missing == 0
    assert r2.body["messages"][3]["content"][0]["tool_use_id"] == "t1"


def test_exact_requests_are_not_marked_approx():
    assert not any(r.approx for r in reconstruct_session(records()))


def test_missing_shape_marks_incomplete():
    recs = [r for r in records() if r["type"] != "api-request-shape"]
    assert not reconstruct_session(recs)[0].complete


def test_timestamps_parsed_to_epoch_seconds():
    r1, r2 = reconstruct_session(records())
    assert r2.ts - r1.ts == 5.0


def test_conversation_from_records_merges_blocks_and_skips_sidechains():
    from reprouter.corpus import conversation_from_records

    recs = records() + [
        {"type": "assistant", "sessionId": "s1", "isSidechain": True,
         "message": {"id": "side", "role": "assistant", "content": [{"type": "text", "text": "subagent"}]}},
    ]
    conv = conversation_from_records(recs)
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["assistant", "user"]  # U1 is only a blob, not a transcript record
    assert [b["type"] for b in conv["messages"][0]["content"]] == ["thinking", "tool_use"]
    assert conv["tools"] == TOOLS and conv["system"] == SYSTEM


def test_conversation_handles_string_user_content():
    from reprouter.corpus import conversation_from_records

    recs = [{"type": "user", "message": {"role": "user", "content": "hello"}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "again"}]}}]
    conv = conversation_from_records(recs)
    assert conv["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"},
                                                             {"type": "text", "text": "again"}]}]
    assert "system" not in conv


def test_load_jsonl_session_skips_bad_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records()) + "\nnot json\n")
    assert len(reconstruct_session(load_jsonl_session(p))) == 2
