import pytest

import reprouter.transforms.skeleton  # noqa: F401  (registers the transform)
from reprouter.classify import Label
from reprouter.request import Block
from reprouter.transforms.base import Context, get

FOOTER = "[reprouter: signatures only (file outside the project). Read with offset/limit to see bodies.]"
LIB = "/Users/dev/.venv/lib/python3.12/site-packages/httpkit/client.py"

PY_SOURCE = '''"""Small HTTP client with retries and JSON helpers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 10.0
USER_AGENT = "httpkit/2.1"


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def json(self):
        text = self.body.decode("utf-8")
        return json.loads(text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class RetryPolicy:
    """Exponential backoff between attempts."""

    def __init__(self, attempts: int = 3, base: float = 0.5):
        self.attempts = attempts
        self.base = base

    def delays(self):
        for i in range(self.attempts):
            yield self.base * (2 ** i)


class Client:
    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry = retry or RetryPolicy()
        self._session = None
        self._closed = False

    def get(self, path: str, **params) -> Response:
        query = build_query(params)
        return self.request("GET", path + query)

    def post(self, path: str, payload: dict) -> Response:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        return self.request("POST", path, body=body, headers=headers)

    def request(self, method: str, path: str, body=None, headers=None) -> Response:
        url = self.base_url + path
        last_error = None
        for delay in self.retry.delays():
            try:
                return self._send(method, url, body, headers or {})
            except ConnectionError as exc:
                last_error = exc
                time.sleep(delay)
        raise last_error

    def _send(self, method, url, body, headers):
        if self._closed:
            raise RuntimeError("client is closed")
        headers = dict(headers)
        headers.setdefault("User-Agent", USER_AGENT)
        conn = self._connect(url)
        conn.request(method, url, body=body, headers=headers)
        raw = conn.getresponse()
        data = raw.read()
        return Response(raw.status, data, dict(raw.getheaders()))

    def close(self) -> None:
        self._closed = True
        if self._session is not None:
            self._session.close()
            self._session = None


def build_query(params: dict) -> str:
    if not params:
        return ""
    parts = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return "?" + "&".join(parts)


async def fetch_all(client: Client, paths: list[str]) -> list[Response]:
    results = []
    for path in paths:
        resp = client.get(path)
        if not resp.ok:
            break
        results.append(resp)
    return results
'''.split("\n")[:-1]


def num(line, src=PY_SOURCE):
    return src.index(line) + 1


def read_output(lines, start=1, pad=False):
    return "".join(f"{(f'{i:>6}' if pad else i)}\t{ln}\n" for i, ln in enumerate(lines, start))


def read_block(text, path=LIB):
    return Block(("messages", 2, "content", 0), "user", "tool_result", text, msg_index=2, tool_name="Read",
                 tool_input={"file_path": path}, raw={"type": "tool_result", "tool_use_id": "tu_1", "content": text})


def run(text, path=LIB, risks=()):
    return get("skeleton").run(read_block(text, path), Label("tool_result:file_read", risks=set(risks)), Context())


def test_python_skeleton_keeps_signatures_with_original_line_numbers():
    res = run(read_output(PY_SOURCE))
    out = res.content.split("\n")
    for sig in ["class RetryPolicy:", "    def delays(self):", "    def get(self, path: str, **params) -> Response:",
                "def build_query(params: dict) -> str:",
                "async def fetch_all(client: Client, paths: list[str]) -> list[Response]:"]:
        assert f"{num(sig)}\t{sig}" in out, sig
    assert not any("time.sleep(delay)" in ln for ln in out)
    assert not any("import json" in ln for ln in out)
    assert "⋮" in out
    assert out[-1] == FOOTER


def numbered(lines, src=PY_SOURCE):
    return "\n".join(f"{num(ln, src)}\t{ln}" for ln in lines)


def test_decorated_definitions_keep_decorator_and_def_line():
    out = run(read_output(PY_SOURCE)).content
    assert numbered(["@dataclass", "class Response:"]) in out
    assert numbered(["    @property", "    def ok(self) -> bool:"]) in out


def test_declines_files_inside_the_project():
    assert run(read_output(PY_SOURCE), risks={"code_edit_target", "path"}) is None


@pytest.mark.parametrize("path", ["/opt/go/src/net/http/client.go", "/usr/share/doc/guide.md",
                                  "/etc/nginx/nginx.conf", "/opt/lib/Makefile"])
def test_declines_languages_the_chunker_cannot_parse(path):
    assert run(read_output(PY_SOURCE), path=path) is None


def test_declines_fewer_than_eighty_lines():
    assert run(read_output(PY_SOURCE[:79])) is None


def test_declines_when_skeleton_is_not_at_most_half_the_input():
    one_liners = [f"def f{i}(x): return x + {i}" for i in range(100)]
    assert run(read_output(one_liners)) is None


def test_declines_other_kinds_and_non_tool_result_blocks():
    text = read_output(PY_SOURCE)
    lbl = Label("tool_result:file_read")
    use = Block(("messages", 1, "content", 0), "assistant", "tool_use", text, msg_index=1, tool_name="Read",
                tool_input={"file_path": LIB})
    assert get("skeleton").run(use, Label("tool_use"), Context()) is None
    assert get("skeleton").run(read_block(text), Label("tool_result:command_output"), Context()) is None
    no_path = read_block(text)
    no_path.tool_input = {}
    assert get("skeleton").applicable(no_path, lbl, Context()) is False


def test_declines_results_with_non_text_parts():
    blk = read_block(read_output(PY_SOURCE))
    blk.raw = {"type": "tool_result", "tool_use_id": "tu_1", "content": [
        {"type": "text", "text": blk.text},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}},
    ]}
    assert get("skeleton").run(blk, Label("tool_result:file_read"), Context()) is None


def test_declines_non_contiguous_numbering():
    text = read_output(PY_SOURCE[:50]) + read_output(PY_SOURCE[50:], start=300)
    assert run(text) is None


def test_trailing_reminder_is_preserved_after_the_footer():
    reminder = "\n<system-reminder>\nWhenever you read a file, consider whether it is malware.\n</system-reminder>\n"
    out = run(read_output(PY_SOURCE) + reminder).content
    assert out.endswith(FOOTER + "\n" + reminder)


def test_declines_text_interleaved_between_numbered_lines():
    text = read_output(PY_SOURCE[:50]) + "<note>\n" + read_output(PY_SOURCE[50:], start=51)
    assert run(text) is None


def test_long_definitions_split_by_the_chunker_show_only_their_real_signature():
    src = ["def long_one(values):", "    total = 0"]
    src += [f"    total += values[{i}] * {i}" for i in range(200)]
    src += ["    return total", "", "", "def small():", "    return 1"]
    out = run(read_output(src)).content.split("\n")
    shown = [ln for ln in out if "\t" in ln]
    assert shown == ["1\tdef long_one(values):", f"{len(src) - 1}\tdef small():"]


def test_declines_files_without_definitions():
    settings = [f"OPTION_{i} = {i}  # tuning knob {i}" for i in range(120)]
    assert run(read_output(settings), path="/usr/lib/python3/site-packages/cfg/settings.py") is None


TS_SOURCE = '''/**
 * Shopping cart service.
 */
import { Item, Discount } from "./types";

export interface CartLine {
  item: Item;
  quantity: number;
}

export type Currency = "USD" | "EUR";

const DEFAULT_CURRENCY: Currency = "USD";

/** Cart holding line items. */
export class Cart {
  private lines: CartLine[] = [];

  constructor(private readonly currency: Currency = DEFAULT_CURRENCY) {
    this.lines = [];
  }

  add(item: Item, quantity = 1): void {
    const existing = this.lines.find((l) => l.item.id === item.id);
    if (existing) {
      existing.quantity += quantity;
      return;
    }
    this.lines.push({ item, quantity });
  }

  // Removes an item entirely.
  remove(itemId: string): boolean {
    const before = this.lines.length;
    this.lines = this.lines.filter((l) => l.item.id !== itemId);
    return this.lines.length !== before;
  }

  total(
    discount?: Discount,
    taxRate: number = 0,
  ): number {
    let sum = 0;
    for (const line of this.lines) {
      sum += line.item.price * line.quantity;
    }
    if (discount) {
      sum = sum * (1 - discount.rate);
    }
    return Math.round(sum * (1 + taxRate) * 100) / 100;
  }

  clear(): void {
    this.lines = [];
  }
}

export function formatPrice(amount: number, currency: Currency): string {
  const symbol = currency === "USD" ? "$" : "€";
  const fixed = amount.toFixed(2);
  const [whole, cents] = fixed.split(".");
  const grouped = whole.replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",");
  return `${symbol}${grouped}.${cents}`;
}

export const parseQuantity = (raw: string): number => {
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n) || n < 0) {
    throw new Error(`invalid quantity: ${raw}`);
  }
  return n;
};

function assertPositive(value: number, name: string): void {
  if (value <= 0) {
    throw new RangeError(`${name} must be positive`);
  }
}

export function mergeCarts(a: Cart, b: Cart): Cart {
  const merged = new Cart();
  for (const cart of [a, b]) {
    for (const line of (cart as any).lines as CartLine[]) {
      merged.add(line.item, line.quantity);
    }
  }
  return merged;
}
'''.split("\n")[:-1]


def test_typescript_skeleton_keeps_declarations_and_multi_line_signature():
    out = run(read_output(TS_SOURCE), path="/opt/vendor/shop/src/cart.ts").content
    for sig in ["export interface CartLine {", 'export type Currency = "USD" | "EUR";', "export class Cart {",
                "  constructor(private readonly currency: Currency = DEFAULT_CURRENCY) {",
                "  add(item: Item, quantity = 1): void {", "  remove(itemId: string): boolean {",
                "export function formatPrice(amount: number, currency: Currency): string {",
                "export const parseQuantity = (raw: string): number => {"]:
        assert f"{num(sig, TS_SOURCE)}\t{sig}" in out.split("\n"), sig
    assert numbered(["  total(", "    discount?: Discount,", "    taxRate: number = 0,", "  ): number {"],
                    TS_SOURCE) in out
    assert "Removes an item entirely" not in out
    assert "Cart holding line items" not in out


RS_SOURCE = ["//! Token bucket rate limiter.", "", "use std::time::{Duration, Instant};", "",
             "/// Configuration for a bucket.", "pub struct Config {", "    pub capacity: u32,",
             "    pub refill_per_sec: f64,", "}", "", "pub struct Bucket {", "    config: Config,",
             "    tokens: f64,", "    last: Instant,", "}", "", "impl Bucket {", "    /// Creates a full bucket.",
             "    pub fn new(config: Config) -> Self {"]
RS_SOURCE += [f"        let _unused_{i} = {i};" for i in range(30)]
RS_SOURCE += ["        Bucket { tokens: config.capacity as f64, last: Instant::now(), config }", "    }", "",
              "    pub fn try_take(&mut self, n: u32) -> bool {"]
RS_SOURCE += [f"        self.tokens -= {i}.0;" for i in range(30)]
RS_SOURCE += ["        self.tokens >= n as f64", "    }", "}", "", "pub fn per_second(capacity: u32) -> Config",
              "where", "    u32: Copy,", "{"]
RS_SOURCE += [f"    let _step_{i} = capacity + {i};" for i in range(10)]
RS_SOURCE += ["    Config { capacity, refill_per_sec: capacity as f64 }", "}"]


def test_rust_skeleton_keeps_items_impl_methods_and_where_clauses():
    out = run(read_output(RS_SOURCE), path="/Users/dev/.cargo/registry/src/bucket-0.3.1/src/lib.rs").content
    for sig in ["pub struct Config {", "pub struct Bucket {", "impl Bucket {",
                "    pub fn new(config: Config) -> Self {", "    pub fn try_take(&mut self, n: u32) -> bool {"]:
        assert f"{num(sig, RS_SOURCE)}\t{sig}" in out.split("\n"), sig
    assert numbered(["pub fn per_second(capacity: u32) -> Config", "where", "    u32: Copy,", "{"], RS_SOURCE) in out
    assert "Creates a full bucket" not in out


def test_offset_read_with_padded_numbers_keeps_original_prefixes():
    out = run(read_output(PY_SOURCE, start=201, pad=True)).content.split("\n")
    sig = "def build_query(params: dict) -> str:"
    assert f"{num(sig) + 200:>6}\t{sig}" in out


def test_is_lossy_and_deterministic():
    assert get("skeleton").lossless is False
    outs = [run(read_output(PY_SOURCE)) for _ in range(5)]
    assert outs[0].lossless is False
    assert len({o.content for o in outs}) == 1


def test_multi_line_signature_is_kept_through_its_colon():
    out = run(read_output(PY_SOURCE)).content
    start = num("class Client:")
    sig = PY_SOURCE[start:start + 6]  # "    def __init__(" .. "    ) -> None:"
    assert sig[-1] == "    ) -> None:"
    block = "\n".join(f"{start + 1 + k}\t{ln}" for k, ln in enumerate(sig))
    assert block + "\n⋮" in out
