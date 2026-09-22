"""Who is allowed to read the whole instrument table.

The listing master brings in roughly 3,950 Korean names that have no price, no
filing and no reason to be asked about. `instrument.tracked` separates those
from the ones we actually follow, and the separation is only worth anything if
every expensive reader applies it.

The failure is quiet in a way that makes a test worth writing. Nothing raises:
the DART collector simply asks for 3,950 companies instead of nine, at about
six calls each, and goes through the day's budget and then the published cap
behind it. The cross-sectional peer group simply grows from nine members to
thousands, and every v0.3 score changes. Both look like ordinary operation.

So this reads the source rather than the behaviour. A behavioural test would
need each collector's network stubbed and would still only cover the callers
someone remembered to write a test for — and the caller nobody remembers is
exactly the one that breaks. Parsing every call site catches the one added
next year by someone who never read this file.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

# News matching is the exception, and the only one. Finding a company worth
# looking at is the entire point of reading the news, so it has to see names
# we do not follow yet — that is what makes them candidates.
UNFILTERED_BY_DESIGN = {"collectors/naver_news.py"}


def call_sites() -> list[tuple[str, int, bool]]:
    """Every `list_active(...)` call in the app, and whether it says `tracked`."""
    found: list[tuple[str, int, bool]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "list_active":
                continue
            relative = path.relative_to(APP).as_posix()
            says = any(kw.arg == "tracked" for kw in node.keywords)
            found.append((relative, node.lineno, says))
    return found


class TestEveryReaderDecides:
    def test_the_call_sites_are_still_there(self) -> None:
        """Guards the test itself: a rename would otherwise empty it."""
        sites = call_sites()

        assert len(sites) >= 5, f"expected several call sites, found {sites}"

    def test_every_caller_but_news_filters_on_tracked(self) -> None:
        silent = [
            f"{path}:{line}"
            for path, line, says in call_sites()
            if not says and path not in UNFILTERED_BY_DESIGN
        ]

        assert silent == [], (
            "these read the whole instrument table and will pick up the "
            f"listing master: {silent}. Pass tracked=True, or add the file to "
            "UNFILTERED_BY_DESIGN with the reason it genuinely needs every row."
        )

    def test_news_matching_still_sees_everything(self) -> None:
        """The exception has to stay an exception, in both directions.

        Filtering here would mean only companies we already follow can ever be
        mentioned, and the discovery this whole phase exists for stops.
        """
        news = [(path, says) for path, _, says in call_sites() if path in UNFILTERED_BY_DESIGN]

        assert news, "naver_news no longer reads the universe at all"
        assert all(not says for _, says in news), news
