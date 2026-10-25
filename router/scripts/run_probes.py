"""Run Haiku fidelity probes for every transform on synthetic + benchmark-repo samples (paid, ledger-gated)."""
import json, sys, time
from pathlib import Path
from reprouter.tokens import load_dotenv
from reprouter.spend import Ledger
from reprouter.fidelity import samples as S
from reprouter.fidelity.run import AnthropicMessagesClient, run_probes
from reprouter.fidelity.score import summarize
from reprouter import transforms  # noqa
from reprouter.transforms.base import registry

load_dotenv()
rich = Path("bench/.cache/repos/rich-9d8f9a372cc5/rich")
src_files = [rich / f for f in ("progress_bar.py", "panel.py", "rule.py", "columns.py")]
reread_files = [rich / f for f in ("padding.py", "_wrap.py", "segment.py")]
samples = (S.json_record_samples(4, seed=11) + S.pytest_samples(4, seed=12) + S.app_log_samples(4, seed=13)
           + S.dedup_samples(4, seed=14) + S.bench_path_samples(4, seed=15)
           + S.source_samples(src_files) + S.reread_samples(reread_files))
out = Path("router/results")
out.mkdir(parents=True, exist_ok=True)
rows_path = out / "fidelity_rows.jsonl"
rows_path.write_text("")
led = Ledger("spend/ledger.jsonl", cap_usd=3.0)
print(f"ledger before: ${led.total():.4f} billed")
def log(row):
    with open(rows_path, "a") as f:
        f.write(json.dumps(row) + "\n")
t0 = time.time()
rows = run_probes(samples, list(registry().values()), AnthropicMessagesClient(), ledger=led,
                  model="claude-haiku-5-5", log=log)
summary = summarize(rows)
(out / "fidelity_summary.json").write_text(json.dumps(summary, indent=2))
print(f"{len(rows)} paired probe rows in {time.time()-t0:.0f}s; ledger now ${led.total():.4f}")
for t, s in sorted(summary.items()):
    print(f"{t:16} n={s['n']:3} orig={s['orig_acc']:.0%} tx={s['tx_acc']:.0%} retained={s['retained']:.0%} lost={s['lost']} gained={s['gained']} p={s['p_value']:.3f}")
