"""판정은 덧붙이고, 덮어쓰지 않는다

`news_query_hit`은 (기사, 종목) 한 행에 판정을 담고 제자리에서 갱신했다. 그러면
09/23에 CONFIRMED였던 판정이 09/24 새 규칙으로 PENDING이 되는 순간 앞의 사실이 사라진다.
`news_mention`도 판정이 바뀌면 행을 지우므로, "그날은 언급으로 취급했다"를 되살릴 곳이
없었다. 판정이 같으면 `decided_at`은 두면서 `snippet`과 `rule_version`만 새 값으로 바꿔서,
판정 시각과 판정 입력의 시점이 어긋난 행도 가능했다. 재무 `semantic_version`을 제자리
갱신했다가 시점이 샌 것과 같은 구조다.

그래서 둘로 나눈다.

    news_query_hit           검색이 그 기사를 찾았다는 사실. 한 번 쓰고 건드리지 않는다
    news_relevance_decision  그 행에 대한 판정 하나하나. 덧붙이기만 한다

판정 시각은 데이터베이스가 쓸 때 찍는다(`clock_timestamp`). 호출자가 시각을 넘기지 않으므로
판정을 실제보다 이르게 기록할 방법이 없다.

**옮겨지는 것은 지금 남아 있는 판정 하나씩뿐이다.** 제자리 갱신으로 이미 덮인 이전 판정은
어디에도 없으므로 되살리지 못한다. 옮긴 행의 시각은 기존 `decided_at` 그대로다.

downgrade는 각 행의 최신 판정을 `news_query_hit`에 다시 붙이고 이력 테이블을 지운다.
그때 이력은 잃는다.

Revision ID: 7d2f4b8e1a36
Revises: 5c1e9a3d7b24
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2f4b8e1a36"
down_revision: str | None = "5c1e9a3d7b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECISION = sa.Enum(
    "CONFIRMED", "PENDING", "REJECTED", name="news_hit_decision", native_enum=False, length=12
)
_METHOD = sa.Enum("NAME", "ALIAS", "SYMBOL", name="news_match_method", native_enum=False, length=16)
_DECIDER = sa.Enum("RULE", "LLM", name="news_decider", native_enum=False, length=8)


def upgrade() -> None:
    op.create_table(
        "news_relevance_decision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("query_hit_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", _DECISION, nullable=False),
        sa.Column("decision_reason", sa.String(length=64), nullable=False),
        sa.Column("match_method", _METHOD, nullable=True),
        sa.Column("matched_query", sa.String(length=200), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("decided_by", _DECIDER, nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["query_hit_id"], ["news_query_hit.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_news_relevance_decision_hit_time",
        "news_relevance_decision",
        ["query_hit_id", "decided_at", "id"],
    )
    op.create_index("ix_news_relevance_decision_time", "news_relevance_decision", ["decided_at"])

    op.execute(
        """
        INSERT INTO news_relevance_decision (
            query_hit_id, decision, decision_reason, match_method, matched_query,
            snippet, rule_version, decided_by, decided_at
        )
        SELECT id, decision, decision_reason, match_method, matched_query,
               snippet, rule_version, decided_by, decided_at
        FROM news_query_hit
        ORDER BY id
        """
    )

    op.drop_index("ix_news_query_hit_instrument_decision", table_name="news_query_hit")
    op.drop_index("ix_news_query_hit_decision_rule", table_name="news_query_hit")
    for column in (
        "decision",
        "decision_reason",
        "match_method",
        "snippet",
        "rule_version",
        "decided_by",
        "decided_at",
    ):
        op.drop_column("news_query_hit", column)
    op.create_index("ix_news_query_hit_instrument", "news_query_hit", ["instrument_id"])


def downgrade() -> None:
    op.drop_index("ix_news_query_hit_instrument", table_name="news_query_hit")
    op.add_column("news_query_hit", sa.Column("decision", _DECISION, nullable=True))
    op.add_column(
        "news_query_hit", sa.Column("decision_reason", sa.String(length=64), nullable=True)
    )
    op.add_column("news_query_hit", sa.Column("match_method", _METHOD, nullable=True))
    op.add_column("news_query_hit", sa.Column("snippet", sa.Text(), nullable=True))
    op.add_column("news_query_hit", sa.Column("rule_version", sa.Integer(), nullable=True))
    op.add_column("news_query_hit", sa.Column("decided_by", _DECIDER, nullable=True))
    op.add_column(
        "news_query_hit", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.execute(
        """
        UPDATE news_query_hit h SET
            decision = d.decision, decision_reason = d.decision_reason,
            match_method = d.match_method, matched_query = d.matched_query,
            snippet = d.snippet, rule_version = d.rule_version,
            decided_by = d.decided_by, decided_at = d.decided_at
        FROM (
            SELECT DISTINCT ON (query_hit_id) *
            FROM news_relevance_decision
            ORDER BY query_hit_id, decided_at DESC, id DESC
        ) d
        WHERE d.query_hit_id = h.id
        """
    )
    # A hit with no verdict cannot exist under the old shape.
    op.execute("DELETE FROM news_query_hit WHERE decision IS NULL")
    for column in ("decision", "decision_reason", "rule_version", "decided_by", "decided_at"):
        op.alter_column("news_query_hit", column, nullable=False)

    op.create_index(
        "ix_news_query_hit_instrument_decision", "news_query_hit", ["instrument_id", "decision"]
    )
    op.create_index(
        "ix_news_query_hit_decision_rule",
        "news_query_hit",
        ["decision", "decided_by", "rule_version"],
    )
    op.drop_index("ix_news_relevance_decision_time", table_name="news_relevance_decision")
    op.drop_index("ix_news_relevance_decision_hit_time", table_name="news_relevance_decision")
    op.drop_table("news_relevance_decision")
