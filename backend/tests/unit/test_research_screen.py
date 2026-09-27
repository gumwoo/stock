"""연구 화면 데이터(frontend/src/research/studies.json)가 원천 문서(docs/studies/*.md)의 판정 표와 같은가.

연구 결과는 DB에 없고 화면이 정적 파일로 보여 준다. 문서의 값이 검토에서 고쳐지면(공시 ±30% 규칙, 휴장일, NaN 등
실제로 여러 번 있었다) 화면이 옛 값을 계속 보여 줄 수 있어서, 칸 단위로 정확히 같은지 확인한다. "들어 있는가"로
대조하면 "성립"이 "성립 안 함"에, "0.07"이 음수 부호가 붙은 "0.07"에 들어 있어 판정이나 부호가 뒤집혀도
통과한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STUDIES = ROOT / "frontend" / "src" / "research" / "studies.json"
FIELDS = ("key", "question", "value", "t", "halves", "holdout", "verdict")


def _cells(line: str) -> list[str]:
    return [c.strip().replace("**", "") for c in line.strip().strip("|").split("|")]


def _section(doc: Path, heading: str) -> list[str]:
    """제목 줄부터 다음 제목(단계와 무관)까지. v2·v3가 v1 절의 하위 제목이라 같은 단계까지 잡으면 섞인다."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    starts = [
        i for i, line in enumerate(lines) if re.fullmatch(r"#{1,6} " + re.escape(heading), line)
    ]
    assert len(starts) == 1, f"{doc.name}: heading {heading!r} found {len(starts)} times"
    start = starts[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"#{1,6} ", lines[i])), len(lines)
    )
    return lines[start + 1 : end]


def _data_rows(section: list[str]) -> list[list[str]]:
    """판정 표의 데이터 행(첫 칸이 질문 키인 행). 머리 행은 첫 칸이 비어 있고 구분 행은 `---`다."""
    rows = []
    for line in section:
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if len(cells) == len(FIELDS) and cells[0] and not set(cells[0]) <= {"-", ":"}:
            rows.append(cells)
    return rows


def _mismatches(studies: list[dict]) -> list[str]:  # type: ignore[type-arg]
    problems = []
    for study in studies:
        table = _data_rows(_section(ROOT / study["doc"], study["section"]))
        if len(table) != len(study["rows"]):
            problems.append(f"{study['id']}: doc {len(table)} rows, screen {len(study['rows'])}")
        for row in study["rows"]:
            matches = [cells for cells in table if cells[0] == row["key"]]
            if len(matches) != 1:
                problems.append(f"{study['id']} {row['key']}: {len(matches)} rows in the doc")
            elif tuple(matches[0]) != tuple(row[f] for f in FIELDS):
                problems.append(
                    f"{study['id']} {row['key']}: screen {[row[f] for f in FIELDS]} != doc {matches[0]}"
                )
    return problems


def _load() -> list[dict]:  # type: ignore[type-arg]
    studies = json.loads(STUDIES.read_text(encoding="utf-8"))
    assert studies, "no studies on the research screen"
    return studies  # type: ignore[no-any-return]


def test_every_screen_row_matches_its_doc_table_cell_by_cell() -> None:
    assert _mismatches(_load()) == []


def test_the_guard_catches_a_flipped_verdict_and_a_flipped_sign() -> None:
    minus = chr(0x2212)
    studies = _load()
    v1 = next(s for s in studies if s["id"] == "disclosure-next-day")
    d2 = next(r for r in v1["rows"] if r["key"] == "D2")
    d3 = next(r for r in v1["rows"] if r["key"] == "D3")
    d3["verdict"] = "성립 안 함"  # 문서는 성립
    d2["t"] = minus + d2["t"]  # 문서는 양수
    problems = _mismatches(studies)
    assert any(" D3:" in p for p in problems) and any(" D2:" in p for p in problems)
