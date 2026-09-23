"""검색 결과마다 판정을 남기고, 언급은 그 판정의 결과로만 만든다

뉴스 롤아웃 10종목 단계에서 두 가지가 드러났다. 원림은 상장사이면서 정원을 뜻하는
일반 단어라서, 확정된 언급 18건 중 7건이 정원 기사였다. 카페24는 네이버가 검색어를
쪼개서 100건 중 96건이 무관한 기사였다. 앞의 것은 "이 기사가 그 회사 이야기인가"라는
판정의 문제이고, 지금 구조로는 그 판정을 남길 곳이 없었다.

그래서 세 층으로 나눈다.

    news_item       발행된 것            사실
    news_query_hit  어느 검색이 가져왔나  사실 + 판정
    news_mention    그 회사 이야기다      판정이 CONFIRMED일 때만

**거부된 검색 결과도 남긴다.** 지금까지는 거부된 기사가 어느 종목 질의로 들어왔는지
기록이 없어서, 종목별 거부율을 나중에 다시 계산할 수 없었다. PENDING만 따로 두는
테이블이었다면 REJECTED는 여전히 사라졌을 것이다.

**키는 (기사, 종목)이고 판정은 덮어쓴다.** 창이 겹치거나 PARTIAL 뒤 다시 읽으면
같은 기사가 같은 종목으로 매번 다시 검색된다. 실행마다 행을 쌓으면 끝없이 늘어난다.
`rule_version`이 있어서 규칙이 바뀌면 이전 규칙이 내린 판정만 골라 재판정할 수 있고,
`decided_by`가 있어서 모델의 판정을 규칙이 덮어쓰지 않는다. `decided_at`은 판정이
바뀔 때만 움직인다. 과거 시점을 재현할 때 나중에 내려진 확정이 그때 존재했던 것처럼
보이면 안 되기 때문이다.

**기존 언급은 옮겨 두고 규칙 버전 0으로 표시한다.** 이 마이그레이션이 판단하지는 않는다.
옮겨진 판정은 `rule_version = 0`이라서 `python -m app.cli rejudge-news`가 현재 규칙으로
다시 판정하고, 판정이 바뀐 것은 `news_mention`에서도 빠진다. 이전 실행에서 거부된
조합은 애초에 기록이 없으므로 옮길 수 없다. 기사 원문은 그대로 둔다.

Revision ID: 8063ed4ea8a7
Revises: 2a58b5a09885
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8063ed4ea8a7"
down_revision: str | None = "2a58b5a09885"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_query_hit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("news_item_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("matched_query", sa.String(length=200), nullable=False),
        sa.Column(
            "decision",
            sa.Enum(
                "CONFIRMED",
                "PENDING",
                "REJECTED",
                name="news_hit_decision",
                native_enum=False,
                length=12,
            ),
            nullable=False,
        ),
        sa.Column("decision_reason", sa.String(length=64), nullable=False),
        sa.Column(
            "match_method",
            sa.Enum(
                "NAME", "ALIAS", "SYMBOL", name="news_match_method", native_enum=False, length=16
            ),
            nullable=True,
        ),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column(
            "decided_by",
            sa.Enum("RULE", "LLM", name="news_decider", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_news_query_hit_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_item_id"],
            ["news_item.id"],
            name=op.f("fk_news_query_hit_news_item_id_news_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_query_hit")),
        sa.UniqueConstraint(
            "news_item_id", "instrument_id", name="uq_news_query_hit_item_instrument"
        ),
    )
    op.create_index(
        "ix_news_query_hit_decision_rule",
        "news_query_hit",
        ["decision", "decided_by", "rule_version"],
        unique=False,
    )
    op.create_index(
        "ix_news_query_hit_instrument_decision",
        "news_query_hit",
        ["instrument_id", "decision"],
        unique=False,
    )

    # 기존 언급을 판정 기록으로 옮긴다. 판정 시각은 언급이 들어온 시각이고, 규칙
    # 버전 0은 "현재 규칙으로 아직 판정하지 않았다"는 뜻이다.
    op.execute(
        """
        INSERT INTO news_query_hit (
            news_item_id, instrument_id, matched_query, decision, decision_reason,
            match_method, rule_version, decided_by, created_at, decided_at
        )
        SELECT news_item_id, instrument_id, matched_query, 'CONFIRMED', 'legacy:mention',
               match_method, 0, 'RULE', ingested_at, ingested_at
        FROM news_mention
        ON CONFLICT (news_item_id, instrument_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # 언급 테이블은 건드리지 않는다. 되돌린 뒤에도 확정된 언급은 그대로 남고,
    # 사라지는 것은 판정 기록과 거부·보류된 검색 결과다.
    op.drop_index("ix_news_query_hit_instrument_decision", table_name="news_query_hit")
    op.drop_index("ix_news_query_hit_decision_rule", table_name="news_query_hit")
    op.drop_table("news_query_hit")
