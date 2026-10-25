import sys, time, json
from reprouter.tokens import load_dotenv, AnthropicCountClient, TokenCounter
from reprouter.measure import load_conversations, run_scoreboard
from reprouter import transforms  # noqa
from reprouter.transforms.base import registry
load_dotenv()
t0 = time.time()
convs = load_conversations()
counter = TokenCounter(AnthropicCountClient(), cache_path="router/.cache/counts.jsonl", model="claude-haiku-5-5")
fid = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else {}
lossless = {n for n, t in registry().items() if t.lossless}
structural = {
    "image": "cannot be delivered live: rewriting a block the model already saw breaks preserved-thinking binding, "
             "and new blocks must stay text (recent turns)",
    "alias": "live delivery needs exact replay of model-written aliases; experimental, off by default",
    "tool_input_elide": "candidate: needs the end-to-end run (does Claude re-read or fail more? does the binding "
                        "canary stay green?) before it can be enabled",
}
sb = run_scoreboard(convs, list(registry().values()), counter, out_dir="router/results",
                    transcripts_root="~/.claude/projects", fidelity=fid, lossless_exempt=lossless,
                    structural=structural, workers=8)
print(f"done in {time.time()-t0:.0f}s: {sb.sessions} sessions, {sb.tool_results} tool results")
