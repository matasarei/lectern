"""`lectern dev agent-check`: send a prompt set to a running server and score the answers.

Each prompt goes to a fresh chat in a new project (with the set's files imported), so no
answer sees another. A prompt passes when the chat used the tools it must (for the
calculation set: code.run) and the answer contains the expected numbers or words.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).parent / "prompts"
TURN_TIMEOUT = 600.0
TOLERANCE = 0.005
REQUIRED_TOOLS = {"calc": ["code.run"]}


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    numbers: tuple[float, ...] = ()
    text: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()  # must be called (any order)
    forbid: tuple[str, ...] = ()  # must not be called


@dataclass(frozen=True)
class Conversation:
    """One chat: `turns` short prompts cycled from `filler`, then `final`, scored."""

    filler: tuple[str, ...]
    turns: int
    final: Case


@dataclass(frozen=True)
class PromptSet:
    name: str
    cases: list[Case]
    files: list[Path]
    conversation: Conversation | None = None


@dataclass
class Outcome:
    case: Case
    tools: list[str] = field(default_factory=list)
    answer: str = ""
    seconds: float = 0.0
    error: str | None = None
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    compactions: int = 0
    checks: dict[str, bool] = field(default_factory=dict)  # extra checks a run adds

    @property
    def used_tools(self) -> bool:
        return all(t in self.tools for t in self.required) and not any(
            t in self.tools for t in self.forbidden
        )

    @property
    def right(self) -> bool:
        return self.error is None and answer_matches(self.case, self.answer)

    @property
    def passed(self) -> bool:
        return self.used_tools and self.right and all(self.checks.values())


def load(name: str) -> PromptSet:
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(p.stem for p in PROMPTS_DIR.glob("*.yaml"))
        raise ValueError(f"No prompt set {name!r}. Sets: {known}.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [_case(item) for item in data.get("prompts", [])]
    conversation = None
    if "conversation" in data:
        spec = data["conversation"]
        conversation = Conversation(
            filler=tuple(" ".join(f.split()) for f in spec["filler"]),
            turns=int(spec["turns"]),
            final=_case(spec["final"]),
        )
    files = [PROMPTS_DIR / "files" / f for f in data.get("files", [])]
    return PromptSet(name, cases, files, conversation)


def load_set(name: str) -> tuple[list[Case], list[Path]]:
    prompt_set = load(name)
    return prompt_set.cases, prompt_set.files


def _case(item: dict) -> Case:
    return Case(
        id=item["id"],
        prompt=" ".join(item["prompt"].split()),
        numbers=tuple(float(n) for n in item.get("numbers", [])),
        text=tuple(str(t) for t in item.get("text", [])),
        tools=tuple(item.get("tools", [])),
        forbid=tuple(item.get("forbid", [])),
    )


# --- files a set needs that are made at run time --------------------------------------------

# The lease the tools and long sets read. Made here, not committed as a binary: every clause
# is visible, and the text a test quotes is this text. All names are made up.
LEASE_TITLE = "Договір оренди нежитлового приміщення"
LEASE_CLAUSES = [
    ("1. Сторони", "Орендодавець: ТОВ «Приклад-Сервіс». Орендар: Ковальчук Олена Петрівна."),
    ("2. Предмет договору", "Приміщення площею 42 кв. м за адресою: м. Київ, вул. Зразкова, 7."),
    ("3. Орендна плата", "3.2. Орендна плата становить 250,00 грн на місяць і сплачується до "
     "10 числа поточного місяця.\n3.3. За кожен день прострочення платежу Орендар сплачує пеню "
     "у розмірі 0,5% від суми заборгованості, включно з днем оплати."),
    ("5. Строк дії", "Договір діє до 31 грудня 1999 року."),
]  # fmt: skip
CLAUSE_3_3 = (
    "За кожен день прострочення платежу Орендар сплачує пеню у розмірі 0,5% від суми "
    "заборгованості, включно з днем оплати."
)


def materialise(files: list[Path], into: Path) -> list[Path]:
    """The set's files: from prompts/files/, or made here (lease-1998.docx)."""
    ready = []
    for path in files:
        if path.is_file():
            ready.append(path)
        elif path.name == "lease-1998.docx":
            ready.append(_write_lease(into / path.name))
        else:
            raise ValueError(f"The prompt set needs {path.name}, which isn't there.")
    return ready


def _write_lease(path: Path) -> Path:
    import docx

    document = docx.Document()
    document.add_heading(LEASE_TITLE, level=1)
    document.add_paragraph("м. Київ, 12 травня 1998 року.")
    for heading, body in LEASE_CLAUSES:
        document.add_heading(heading, level=2)
        for paragraph in body.split("\n"):
            document.add_paragraph(paragraph)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path


# --- scoring ---------------------------------------------------------------------------------

# A comma, dot or apostrophe joins digits only when digits follow at once ("4,172.04");
# a space joins only a group of exactly three digits ("4 172,04"). So "300, 500 and 200"
# stays three numbers.
_NUMBER = re.compile("-?\\d+(?:[.,']\\d+|[ \u00a0\u202f]\\d{3}(?!\\d))*")


def numbers_in(text: str) -> list[float]:
    """Every number in `text`, read the ways people write them: 4,172.04 · 4 172,04 ·
    4172.04 · 62,50. A lone comma before exactly three digits is a thousands separator."""
    found: list[float] = []
    for raw in _NUMBER.findall(text):
        token = re.sub("[\u00a0\u202f ']", "", raw)
        if "," in token and "." in token:
            decimal = "," if token.rfind(",") > token.rfind(".") else "."
            thousands = "." if decimal == "," else ","
            token = token.replace(thousands, "").replace(decimal, ".")
        elif "," in token:
            parts = token.split(",")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                token = token.replace(",", "")
            else:
                token = token.replace(",", ".")
        elif token.count(".") > 1:
            token = token.replace(".", "")
        try:
            found.append(float(token))
        except ValueError:
            continue
    return found


def answer_matches(case: Case, answer: str) -> bool:
    found = numbers_in(answer)
    for expected in case.numbers:
        if not any(abs(value - expected) <= TOLERANCE for value in found):
            return False
    lowered = answer.casefold()
    return all(word.casefold() in lowered for word in case.text)


# --- running ---------------------------------------------------------------------------------


def run_set(client, name: str, *, allow_cards: bool, only: list[str] | None = None,
            echo=print) -> list[Outcome]:  # noqa: ANN001  # fmt: skip
    """Run the set against a running server through `client` (an httpx2.Client with the
    token and Origin headers). Cards are answered Allow when `allow_cards`, else Don't allow."""
    prompt_set = load(name)
    cases = [c for c in prompt_set.cases if not only or c.id in only]
    project = client.post("/api/projects", json={"name": f"agent-check {name}"}).json()
    pid = project["id"]
    with tempfile.TemporaryDirectory() as made:
        for path in materialise(prompt_set.files, Path(made)):
            response = client.put(
                f"/api/projects/{pid}/files", params={"name": path.name}, content=path.read_bytes()
            )
            response.raise_for_status()
    outcomes = []
    for case in cases:
        chat = client.post(f"/api/projects/{pid}/sessions", json={"title": case.id}).json()
        outcome = _ask(client, pid, chat["id"], case, allow_cards=allow_cards)
        outcome.required = case.tools or tuple(REQUIRED_TOOLS.get(name, ()))
        outcome.forbidden = case.forbid
        outcomes.append(outcome)
        echo(_line(outcome))
    if prompt_set.conversation and (not only or prompt_set.conversation.final.id in only):
        outcome = run_conversation(
            client, pid, prompt_set.conversation, allow_cards=allow_cards, echo=echo
        )
        outcomes.append(outcome)
        echo(_line(outcome))
    return outcomes


def run_conversation(client, pid: int, conversation: Conversation, *, allow_cards: bool,
                     echo=print) -> Outcome:  # noqa: ANN001  # fmt: skip
    """`turns` filler prompts in one chat, then the final prompt. Passes when the final answer
    is right, compaction ran at least once, and the transcript still has every message."""
    chat = client.post(
        f"/api/projects/{pid}/sessions", json={"title": conversation.final.id}
    ).json()
    compactions = 0
    started = time.monotonic()
    for i in range(conversation.turns):
        filler = Case(f"turn-{i + 1}", conversation.filler[i % len(conversation.filler)])
        step = _ask(client, pid, chat["id"], filler, allow_cards=allow_cards)
        compactions += step.compactions
        if step.error:
            echo(f"  turn {i + 1}: error: {step.error}")
        if (i + 1) % 10 == 0:
            echo(f"  {i + 1}/{conversation.turns} turns · compactions so far: {compactions}")
    outcome = _ask(client, pid, chat["id"], conversation.final, allow_cards=allow_cards)
    outcome.compactions += compactions
    outcome.required = conversation.final.tools
    outcome.forbidden = conversation.final.forbid
    outcome.seconds = round(time.monotonic() - started, 1)
    transcript = client.get(f"/api/projects/{pid}/sessions/{chat['id']}").json()
    asked = sum(1 for m in transcript["messages"] if m["role"] == "user")
    outcome.checks = {
        "compacted": outcome.compactions > 0,
        "transcript kept": asked == conversation.turns + 1,
    }
    return outcome


def _ask(client, pid: int, session_id: int, case: Case, *, allow_cards: bool) -> Outcome:  # noqa: ANN001
    """Send one message to a chat and follow the turn to its end."""
    base = f"/api/projects/{pid}/sessions/{session_id}"
    outcome = Outcome(case)
    started = time.monotonic()
    sent = client.post(f"{base}/messages", json={"text": case.prompt})
    if sent.status_code != 202:
        outcome.error = f"send failed: {sent.status_code} {sent.text[:200]}"
        return outcome
    # Sending clears the chat's event buffer before the turn starts, so everything the stream
    # replays belongs to this turn, even if it finished before the stream connected.
    with client.stream("GET", f"{base}/events", timeout=TURN_TIMEOUT) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line.removeprefix("data: "))
            kind = event.get("type")
            if kind == "tool":
                outcome.tools.append(event["name"])
            elif kind == "delta":
                outcome.answer += event["text"]
            elif kind == "assistant" and event.get("tool_calls"):
                outcome.answer = ""  # text before a tool call isn't the answer
            elif kind == "confirm":
                client.post(
                    f"{base}/confirmations/{event['confirmation_id']}",
                    json={"approve": allow_cards},
                )
            elif kind == "compacted":
                outcome.compactions += 1
            elif kind == "error":
                outcome.error = event["detail"]
            elif kind == "turn" and event.get("status") != "running":
                break
            if time.monotonic() - started > TURN_TIMEOUT:
                outcome.error = "timed out"
                client.post(f"{base}/stop")
                break
    outcome.seconds = round(time.monotonic() - started, 1)
    return outcome


def _line(o: Outcome) -> str:
    mark = "PASS" if o.passed else "FAIL"
    failed = [name for name, ok in o.checks.items() if not ok]
    if o.forbidden and any(t in o.tools for t in o.forbidden):
        failed.append("called a forbidden tool")
    tools = ",".join(t.split(".")[-1] if t.startswith("code.") else t for t in o.tools) or "-"
    answer = " ".join(o.answer.split())[:90]
    note = f" · error: {o.error}" if o.error else ""
    if failed:
        note += " · failed: " + ", ".join(failed)
    if o.compactions:
        note += f" · compactions: {o.compactions}"
    return f"{mark} {o.case.id:<13} {o.seconds:>6.1f}s  tools: {tools:<28} {answer}{note}"


def summary(outcomes: list[Outcome]) -> str:
    total = len(outcomes)
    used = sum(o.used_tools for o in outcomes)
    right = sum(o.right for o in outcomes)
    passed = sum(o.passed for o in outcomes)
    requirements = {o.required for o in outcomes}
    if len(requirements) == 1:
        required = ", ".join(next(iter(requirements))) or "none"
        tools = f"required tools ({required}) used in {used}/{total}"
    else:
        tools = f"right tools in {used}/{total}"
    return f"{passed}/{total} passed · {tools} · right answer in {right}/{total}"
