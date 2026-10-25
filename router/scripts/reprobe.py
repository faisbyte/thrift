"""Re-probe selected transforms and replace their rows in fidelity_rows.jsonl (paid, ledger-gated)."""
import json, sys
from pathlib import Path
from reprouter.tokens import load_dotenv
from reprouter.spend import Ledger
from reprouter.fidelity import samples as S
from reprouter.fidelity.run import AnthropicMessagesClient, run_probes
from reprouter.fidelity.score import summarize
from reprouter import transforms  # noqa
from reprouter.transforms.base import get
load_dotenv()
names = sys.argv[1].split(",")
rich = Path("bench/.cache/repos/rich-9d8f9a372cc5/rich")
samples = (S.pytest_samples(4, seed=12) + S.app_log_samples(4, seed=13)
           + S.source_samples([rich / f for f in ("progress_bar.py", "panel.py", "rule.py", "columns.py", "segment.py")]))
led = Ledger("spend/ledger.jsonl", cap_usd=3.0)
rows = run_probes(samples, [get(n) for n in names], AnthropicMessagesClient(), ledger=led, model="claude-haiku-5-5")
p = Path("router/results/fidelity_rows.jsonl")
old = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
merged = [r for r in old if r["transform"] not in names] + rows
p.write_text("\n".join(json.dumps(r) for r in merged) + "\n")
s = summarize(merged)
Path("router/results/fidelity_summary.json").write_text(json.dumps(s, indent=2))
print(f"replaced {len(rows)} rows; ledger ${led.total():.4f}")
for n in names:
    x = s[n]
    print(f"{n:14} all {x['retained']:.0%} (n={x['n_paired']}) promised {x['promised']['retained']:.0%} (n={x['promised']['n_paired']}) "
          + ", ".join(f"{k} {v['retained']:.0%}/{v['n_paired']}" for k, v in x['by_focus'].items()))
