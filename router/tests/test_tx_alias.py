import random
import re

from rr_fixtures import claude_code_request

from reprouter.classify import classify_request
from reprouter.transforms import alias
from reprouter.transforms.base import Context, get

T = get("alias")
ROOT = "/private/var/folders/05/19_8mk3j7zs8d21kh97f93dm0000gn/T/thrift-bench/edit-padding-css__baseline__0/"
OTHER = "/usr/local/lib/python3.12/site-packages/"


def convo(*calls):
    """Build a Claude Code-shaped request from (tool_name, input, output) tuples (ids tu_1, tu_2, ...)."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "please do the task"}]}]
    for i, (name, inp, out) in enumerate(calls, 1):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"tu_{i}", "name": name, "input": inp}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu_{i}", "content": out}]})
    req = claude_code_request()
    req["messages"] = msgs
    return req


def run_at(req, tool_use_id):
    """applicable + apply without run()'s exception swallowing, so a crash can't pass as a decline."""
    pairs = classify_request(req)
    for i, (b, lab) in enumerate(pairs):
        if b.type == "tool_result" and b.tool_use_id == tool_use_id:
            ctx = Context(project_root=ROOT, prior=pairs[:i])
            return T.apply(b, lab, ctx) if T.applicable(b, lab, ctx) else None
    raise AssertionError(f"no tool_result {tool_use_id}")


def session():
    return convo(
        ("Bash", {"command": f"ls {ROOT}"}, f"{ROOT}a.css\n{ROOT}b.css\n"),
        ("Read", {"file_path": f"{ROOT}a.css"}, "1\tbody { padding: 0 }\n"),
        ("Bash", {"command": "grep -rn padding ."}, f"{ROOT}b.css:3: padding: 4px\n"),
    )


def test_first_block_using_an_alias_carries_the_legend():
    res = run_at(session(), "tu_1")
    assert res.content == f"[reprouter aliases: §1={ROOT[:-1]}]\n§1/a.css\n§1/b.css\n"
    assert res.lossless is True
    assert T.name == "alias" and T.lossless is True


def test_later_block_uses_alias_without_repeating_legend():
    assert run_at(session(), "tu_3").content == "§1/b.css:3: padding: 4px\n"


def test_block_where_no_alias_applies_declined():
    assert run_at(session(), "tu_2") is None


def test_alias_created_later_is_defined_in_first_block_that_uses_it():
    req = convo(
        ("Bash", {"command": f"ls {ROOT}"}, f"{ROOT}a.css\n{ROOT}b.css\n"),
        ("Bash", {"command": f"ls {OTHER}"}, f"{OTHER}six.py\n{OTHER}attr/__init__.py\n{ROOT}c.css\n"),
        ("Bash", {"command": "find / -name '*.py'"}, f"{OTHER}x.py\n{ROOT}d.css\n"),
    )
    assert run_at(req, "tu_2").content == (
        f"[reprouter aliases: §2={OTHER[:-1]}]\n§2/six.py\n§2/attr/__init__.py\n§1/c.css\n"
    )
    assert run_at(req, "tu_3").content == "§2/x.py\n§1/d.css\n"


def test_tool_use_inputs_count_towards_repetition():
    req = convo(
        ("Read", {"file_path": f"{ROOT}a.css"}, "1\tbody {}\n"),
        ("Edit", {"file_path": f"{ROOT}a.css", "old_string": "a", "new_string": "b"}, "ok"),
        ("Bash", {"command": "pwd"}, ROOT + "\n"),
    )
    assert run_at(req, "tu_3").content == f"[reprouter aliases: §1={ROOT[:-1]}]\n§1/\n"


def test_block_containing_the_sigil_declined():
    req = convo(
        ("Bash", {"command": f"ls {ROOT}"}, f"{ROOT}a.css\n{ROOT}b.css\n"),
        ("Bash", {"command": "cat notes"}, f"§ 3 says see {ROOT}a.css\n"),
    )
    assert run_at(req, "tu_1") is not None
    assert run_at(req, "tu_2") is None


def test_non_tool_result_blocks_declined():
    req = session()
    req["messages"].append({"role": "user", "content": [{"type": "text", "text": f"{ROOT}a {ROOT}b {ROOT}c"}]})
    req["messages"].append({"role": "assistant", "content": [{"type": "text", "text": f"see {ROOT}a.css"}]})
    pairs = classify_request(req)
    for i, (b, lab) in enumerate(pairs):
        if b.type != "tool_result":
            assert T.run(b, lab, Context(prior=pairs[:i])) is None


def test_deterministic_for_identical_inputs():
    a, b = run_at(session(), "tu_3"), run_at(session(), "tu_3")
    assert a == b and a is not None


def gen_session(seed, turns=10):
    rng = random.Random(seed)
    texts = gen_texts(seed, n=turns)
    calls = []
    for i, out in enumerate(texts):
        root = rng.choice([ROOT, OTHER, "/home/runner/work/repo/repo/packages/core/"])
        name, inp = rng.choice([("Read", {"file_path": f"{root}f{i}.py"}), ("Bash", {"command": f"ls {root}"}),
                                ("Grep", {"pattern": "x", "path": root})])
        calls.append((name, inp, out))
    return convo(*calls)


_LEGEND = re.compile(r"\[reprouter aliases: ([^\]]*)\]\n")


def outputs(req):
    """tool_use_id -> (original text, transformed content or None) for every tool_result block."""
    pairs = classify_request(req)
    out = {}
    for i, (b, lab) in enumerate(pairs):
        if b.type == "tool_result":
            res = T.run(b, lab, Context(prior=pairs[:i]))
            out[b.tool_use_id] = (b.text, res.content if res else None)
    return out


def test_generated_sessions_define_each_alias_once_before_use_and_round_trip():
    aliased_blocks = 0
    for seed in range(150):
        req = gen_session(seed)
        defined = {}
        for original, content in outputs(req).values():
            if content is None:
                continue
            aliased_blocks += 1
            m = _LEGEND.match(content)
            body = content[m.end():] if m else content
            for entry in (m.group(1).split(" ") if m else []):
                name, prefix = entry.split("=", 1)
                assert name not in defined, (seed, "alias defined twice", name)
                defined[name] = prefix
            table = {name + "/": prefix + "/" for name, prefix in defined.items()}
            sigil = next(iter(defined))[0]  # one sigil per session; the other may occur literally
            used = set(re.findall(re.escape(sigil) + r"\d+", body))
            assert used <= set(defined), (seed, "alias used before its legend", used - set(defined))
            assert alias.expand_aliases(body, table) == original, seed
    assert aliased_blocks > 100


def test_earlier_blocks_render_identically_when_later_turns_are_appended():
    for seed in range(100):
        full = gen_session(seed)
        later = outputs(full)
        for cut in (3, 7, 13):
            partial = dict(full, messages=full["messages"][:cut])
            for tid, rendered in outputs(partial).items():
                assert later[tid] == rendered, (seed, cut, tid)


def test_result_with_image_content_declined():
    content = [{"type": "text", "text": f"{ROOT}a {ROOT}b {ROOT}c"}, {"type": "image", "source": {"type": "base64", "data": "AA"}}]
    assert run_at(convo(("Read", {"file_path": f"{ROOT}x.png"}, content)), "tu_1") is None


def test_build_table_aliases_prefix_repeated_three_times():
    texts = [f"Read {ROOT}src/app.css", f"edited {ROOT}src/app.css ok", f"{ROOT}index.html"]
    assert alias.build_table(texts) == {"§1/": ROOT}


def test_build_table_ignores_prefixes_seen_fewer_than_three_times():
    assert alias.build_table([f"{ROOT}a.css", f"{ROOT}b.css"]) == {}


def test_build_table_ignores_prefixes_shorter_than_24_chars():
    assert alias.build_table(["/a/b/c/x.py " * 10]) == {}
    p24 = "/workspace/projects/abc/"
    assert len(p24) == 24
    assert alias.build_table([f"{p24}x.py " * 3]) == {"§1/": p24}


def test_build_table_picks_several_prefixes_without_redundant_ancestors():
    text = f"{ROOT}a.css {OTHER}six.py {ROOT}b.css {OTHER}attr/x.py {ROOT}c.css {OTHER}y.py {ROOT}d.css {OTHER}z.py"
    assert alias.build_table([text]) == {"§1/": ROOT, "§2/": OTHER}


def test_build_table_prefers_larger_count_times_length():
    deep = ROOT + "src/components/"
    text = f"{deep}a.tsx " * 10 + f"{ROOT}README.md"
    # deep: 10 * 110 = 1100 beats ROOT: 11 * 95 = 1045; ROOT then has only 1 uncovered use left.
    assert alias.build_table([text]) == {"§1/": deep}


def test_build_table_uses_fallback_sigil_when_section_sign_occurs():
    texts = ["see § 4.2 of the spec", f"{ROOT}a {ROOT}b {ROOT}c"]
    assert alias.build_table(texts) == {"¤1/": ROOT}


def test_build_table_empty_when_no_sigil_is_free():
    assert alias.build_table(["§ and ¤", f"{ROOT}a {ROOT}b {ROOT}c"]) == {}


def test_build_table_keeps_earlier_numbering_when_new_prefix_qualifies_later():
    texts = [f"{OTHER}a.py", f"{ROOT}a {ROOT}b {ROOT}c", f"{OTHER}b.py {OTHER}c.py"]
    assert alias.build_table(texts[:2]) == {"§1/": ROOT}
    assert alias.build_table(texts) == {"§1/": ROOT, "§2/": OTHER}


def test_build_table_keeps_sigil_when_it_appears_after_table_exists():
    texts = [f"{ROOT}a {ROOT}b {ROOT}c", "see § 4.2 of the spec"]
    assert alias.build_table(texts) == {"§1/": ROOT}


def test_build_table_later_uses_of_an_aliased_prefix_add_no_redundant_aliases():
    texts = [f"{ROOT}a {ROOT}b {ROOT}c", " ".join(f"{ROOT}src/f{k}" for k in range(6))]
    assert alias.build_table(texts) == {"§1/": ROOT}


def test_build_table_never_replaces_a_chosen_prefix():
    texts = [f"{ROOT}src/a {ROOT}src/b {ROOT}src/c", " ".join(f"{ROOT}lib/{k}" for k in range(5))]
    assert alias.build_table(texts) == {"§1/": ROOT + "src/", "§2/": ROOT + "lib/"}


def gen_texts(seed, n=12):
    """Random block texts mixing long repeated roots, short paths, prose and occasional sigils."""
    rng = random.Random(seed)
    roots = [ROOT, OTHER, ROOT + "src/components/", "/home/runner/work/repo/repo/packages/core/",
             "/Users/someone/Library/Caches/pip/http/", f"/tmp/build-{seed}/output/artifacts/"]
    texts = []
    for _ in range(n):
        parts = []
        for _ in range(rng.randint(0, 8)):
            kind = rng.random()
            if kind < 0.6:
                parts.append(rng.choice(roots) + rng.choice(["a.py", "b/c.ts", "x", "d/e/f.md", ""]))
            elif kind < 0.7:
                parts.append(rng.choice(["/a/b/", "/mnt" + ROOT + "z", "§", "¤", "1/", "§1/"]))
            else:
                parts.append(rng.choice(["ok", "error: not found", "\n", "\t12\t", "x=1", "(", ")"]))
        texts.append(rng.choice([" ", "", "\n", ":"]).join(parts))
    return texts


def test_build_table_is_prefix_stable_for_generated_histories():
    for seed in range(200):
        texts = gen_texts(seed)
        for i in range(len(texts)):
            before, after = alias.build_table(texts[:i]), alias.build_table(texts[: i + 1])
            assert before.items() <= after.items(), (seed, i)


TABLE = {"§1/": ROOT, "§2/": OTHER}


def test_expand_aliases_in_string():
    assert alias.expand_aliases("§1/src/app.css and §2/six.py", TABLE) == f"{ROOT}src/app.css and {OTHER}six.py"


def test_expand_bare_alias_without_slash_means_directory_itself():
    assert alias.expand_aliases("cd §1 && ls §2", TABLE) == f"cd {ROOT[:-1]} && ls {OTHER[:-1]}"


def test_expand_leaves_unknown_aliases_and_other_text_alone():
    assert alias.expand_aliases("§9/x ¤1/y § 4.2", TABLE) == "§9/x ¤1/y § 4.2"
    assert alias.expand_aliases("¤1/y", {"¤1/": ROOT}) == f"{ROOT}y"


def test_expand_aliases_recurses_into_lists_and_dicts_without_mutating():
    value = {"file_path": "§1/a.css", "edits": [{"old_string": "x", "new_string": "§2/b"}],
             "limit": 10, "replace_all": False, "pages": None, "§1/key": "kept"}
    out = alias.expand_aliases(value, TABLE)
    assert out == {"file_path": f"{ROOT}a.css", "edits": [{"old_string": "x", "new_string": f"{OTHER}b"}],
                   "limit": 10, "replace_all": False, "pages": None, "§1/key": "kept"}
    assert value["file_path"] == "§1/a.css" and value["edits"][0]["new_string"] == "§2/b"


def test_expand_with_empty_table_is_identity():
    assert alias.expand_aliases({"a": ["§1/x"]}, {}) == {"a": ["§1/x"]}


def test_apply_aliases_replaces_prefixes_and_reports_used_aliases():
    assert alias.apply_aliases(f"{ROOT}a.css\n{OTHER}b.py", TABLE) == ("§1/a.css\n§2/b.py", ["§1/", "§2/"])
    assert alias.apply_aliases("nothing here", TABLE) == ("nothing here", [])


def test_apply_aliases_prefers_longest_prefix_and_lists_used_in_number_order():
    deep = ROOT + "src/components/"
    table = {"§1/": deep, "§2/": ROOT}
    assert alias.apply_aliases(f"{ROOT}y {deep}x.tsx", table) == ("§2/y §1/x.tsx", ["§1/", "§2/"])


def test_apply_aliases_skips_prefix_embedded_in_a_longer_path():
    text = f"/mnt{ROOT}z and x{ROOT}z"
    assert alias.apply_aliases(text, TABLE) == (text, [])


def test_expand_inverts_apply_for_generated_texts():
    checked = 0
    for seed in range(300):
        texts = gen_texts(seed)
        table = alias.build_table(texts)
        sigil = next(iter(table))[0] if table else None
        for text in texts:
            if sigil and sigil in text:
                continue  # the transform declines such blocks
            aliased, used = alias.apply_aliases(text, table)
            assert alias.expand_aliases(aliased, table) == text, (seed, text)
            assert len(aliased) <= len(text)
            checked += bool(used)
    assert checked > 200  # the property was exercised on many aliased texts


def test_build_table_caps_at_8_aliases_numbered_by_first_appearance():
    roots = [f"/opt/projects/project-number-{i:02d}/workspace/" for i in range(10)]
    text = " ".join(f"{r}f{k}.py" for i, r in enumerate(roots) for k in range(i + 3))
    table = alias.build_table([text])
    # roots 2..9 have the largest count*length; numbering follows first appearance, not score
    assert table == {f"§{n}/": roots[n + 1] for n in range(1, 9)}
