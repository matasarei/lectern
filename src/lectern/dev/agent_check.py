"""`lectern dev agent-check`: send a prompt set to a running server and score the answers.

Each prompt goes to a fresh chat in a new project (with the set's files imported), so no
answer sees another. A prompt passes when the chat used the tools it must (for the
calculation set: code.run) and the answer contains the expected numbers or words.
"""

from __future__ import annotations

import json
import re
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


@dataclass
class Outcome:
    case: Case
    tools: list[str] = field(default_factory=list)
    answer: str = ""
    seconds: float = 0.0
    error: str | None = None
    required: tuple[str, ...] = ()

    @property
    def used_tools(self) -> bool:
        return all(t in self.tools for t in self.required)

    @property
    def right(self) -> bool:
        return self.error is None and answer_matches(self.case, self.answer)

    @property
    def passed(self) -> bool:
        return self.used_tools and self.right


def load_set(name: str) -> tuple[list[Case], list[Path]]:
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(p.stem for p in PROMPTS_DIR.glob("*.yaml"))
        raise ValueError(f"No prompt set {name!r}. Sets: {known}.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [
        Case(
            id=item["id"],
            prompt=" ".join(item["prompt"].split()),
            numbers=tuple(float(n) for n in item.get("numbers", [])),
            text=tuple(str(t) for t in item.get("text", [])),
        )
        for item in data["prompts"]
    ]
    files = [PROMPTS_DIR / "files" / f for f in data.get("files", [])]
    return cases, files


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
    cases, files = load_set(name)
    if only:
        cases = [c for c in cases if c.id in only]
    project = client.post("/api/projects", json={"name": f"agent-check {name}"}).json()
    pid = project["id"]
    for path in files:
        response = client.put(
            f"/api/projects/{pid}/files", params={"name": path.name}, content=path.read_bytes()
        )
        response.raise_for_status()
    outcomes = []
    for case in cases:
        outcome = _run_case(client, pid, case, allow_cards=allow_cards)
        outcome.required = tuple(REQUIRED_TOOLS.get(name, ()))
        outcomes.append(outcome)
        echo(_line(outcome))
    return outcomes


def _run_case(client, pid: int, case: Case, *, allow_cards: bool) -> Outcome:  # noqa: ANN001
    chat = client.post(f"/api/projects/{pid}/sessions", json={"title": case.id}).json()
    base = f"/api/projects/{pid}/sessions/{chat['id']}"
    outcome = Outcome(case)
    started = time.monotonic()
    sent = client.post(f"{base}/messages", json={"text": case.prompt})
    if sent.status_code != 202:
        outcome.error = f"send failed: {sent.status_code} {sent.text[:200]}"
        return outcome
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
    tools = ",".join(t.split(".")[-1] if t.startswith("code.") else t for t in o.tools) or "-"
    answer = " ".join(o.answer.split())[:90]
    note = f" · error: {o.error}" if o.error else ""
    return f"{mark} {o.case.id:<13} {o.seconds:>6.1f}s  tools: {tools:<28} {answer}{note}"


def summary(outcomes: list[Outcome]) -> str:
    total = len(outcomes)
    used = sum(o.used_tools for o in outcomes)
    right = sum(o.right for o in outcomes)
    passed = sum(o.passed for o in outcomes)
    required = ", ".join(outcomes[0].required) if outcomes and outcomes[0].required else "none"
    return (
        f"{passed}/{total} passed · required tools ({required}) used in {used}/{total} · "
        f"right answer in {right}/{total}"
    )
