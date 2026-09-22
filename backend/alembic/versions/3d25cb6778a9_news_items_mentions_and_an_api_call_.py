"""뉴스와 언급, 그리고 호출을 세는 원장

Phase 4는 종목을 워치리스트에서 고르는 대신 뉴스에서 찾으려 한다. 그러려면 두 가지가 먼저
있어야 한다. 뉴스를 담을 자리, 그리고 무료 API의 한도를 절대 넘지 않는다는 보장이다.

**`api_call_bucket`은 후자다.** 지금까지 이 저장소가 가진 유일한 제어는 초당 토큰버킷이고,
토큰은 무한히 리필되므로 루프 하나가 하루치 한도를 몇 분 만에 태울 수 있다. DART는 서버가
거절 코드를 줘야 초과를 알아차린다 — 넘고 나서 아는 구조다.

원장은 (쿼터 그룹, endpoint, 분) 단위로 호출 수를 센다. 합산은 **항상 그룹으로** 한다.
네이버의 25,000회/일은 클라이언트 ID 기준이고 검색 계열 전체가 공유하므로, 뉴스와 블로그를
따로 세면 같은 공식 한도를 두 번 쓰게 된다. `endpoint`는 "무엇이 예산을 먹었나"를 사후에
볼 수 있게만 하고 윈도 술어로는 쓰이지 않는다.

달력 윈도는 만들지 않는다. 달력일 한도가 세는 것은 마지막 리셋부터 지금까지 쓴 양이고,
리셋은 어떤 타임존에서든 최소 24시간마다 일어나므로 그 구간은 항상 롤링 24시간 안에 들어간다.
따라서 롤링 집계가 예산 이하이면 리셋 시각을 몰라도 한도를 넘을 수 없다. 월말·윤년·DST 산술이
전부 존재하지 않는 코드가 된다. 분 단위 버킷은 경계 분을 통째로 세어 항상 과대 집계하는데,
그것이 절대 초과 금지에서 허용되는 유일한 오차 방향이다.

**`news_item`과 `news_mention`이 둘로 나뉜 이유.** 같은 반도체 기사가 삼성전자 질의와
SK하이닉스 질의에 모두 걸린다. 한 테이블이면 본문이 두 번 저장되고, 곧 붙을 감성 스코어러가
같은 글을 두 번 읽어 두 개의 다른 점수를 낼 수도 있다. `match_method`가 있는 이유도 같은
계열이다 — 검색 결과라는 사실만으로 언급을 확정하면 엉뚱한 회사에 점수가 붙고, 그 점수는
완전히 정상으로 보인다.

**`instrument.tracked`는 유니버스가 커질 때 점수가 조용히 바뀌는 것을 막는다.** 한국 상장사
이름 마스터를 넣으면 행이 2,500개가 되는데, 횡단면 동종군은 시장의 활성 종목 전부를 쓰므로
동종군이 9개에서 2,500개로 바뀌고 v0.3 점수가 전부 달라진다. 가격도 재무도 없는 이름을
동종군에 넣는 것은 비교가 아니다. 채점과 순위는 `tracked` 행만 읽고, 뉴스 매칭은 전체를 본다.
기존 18종목은 실제로 수집 대상이므로 true로 채운다.

`instrument.listing`은 README가 네 번 미뤄둔 항목이다. `market`은 KR/US뿐이라 KOSPI와 KOSDAQ을
구분하지 못했고, 그래서 yfinance 티커가 `.KS`로 고정돼 있었다. 지금은 NULL로 두고 마스터
적재와 시드가 채운다.

Revision ID: 3d25cb6778a9
Revises: d9b41c7e5a20
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "3d25cb6778a9"
down_revision = "d9b41c7e5a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 원장만 조건부로 만든다. `downgrade()`가 이 테이블을 지우지 않기 때문에,
    # 되돌렸다가 다시 올리면 이미 있는 테이블을 만나게 된다. 이유는 downgrade
    # 쪽에 적었다.
    if not sa.inspect(op.get_bind()).has_table("api_call_bucket"):
        _create_api_call_bucket()

    _create_news_tables()


def _create_api_call_bucket() -> None:
    op.create_table(
        "api_call_bucket",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("quota_group", sa.String(length=24), nullable=False),
        sa.Column("endpoint", sa.String(length=32), nullable=False),
        sa.Column("minute_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_call_bucket")),
        sa.UniqueConstraint(
            "quota_group", "endpoint", "minute_start", name="uq_api_call_bucket_slot"
        ),
    )
    op.create_index(
        "ix_api_call_bucket_window",
        "api_call_bucket",
        ["quota_group", "minute_start"],
        unique=False,
    )


def _create_news_tables() -> None:

    op.create_table(
        "news_item",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "NAVER_NEWS",
                "THREADS",
                "REDDIT",
                name="news_source",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("naver_url", sa.String(length=1000), nullable=True),
        sa.Column("publisher_host", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_item")),
        sa.UniqueConstraint("source", "url_hash", name="uq_news_item_source_url_hash"),
    )
    op.create_index("ix_news_item_available", "news_item", ["available_at"], unique=False)

    op.create_table(
        "news_mention",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("news_item_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("matched_query", sa.String(length=200), nullable=False),
        sa.Column(
            "match_method",
            sa.Enum(
                "NAME",
                "ALIAS",
                "SYMBOL",
                name="news_match_method",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_news_mention_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_item_id"],
            ["news_item.id"],
            name=op.f("fk_news_mention_news_item_id_news_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_mention")),
        sa.UniqueConstraint(
            "news_item_id", "instrument_id", name="uq_news_mention_item_instrument"
        ),
    )
    op.create_index(
        "ix_news_mention_instrument_item",
        "news_mention",
        ["instrument_id", "news_item_id"],
        unique=False,
    )

    # Autogenerate proposed dropping `uq_backtest_window_one_holdout_per_run`
    # here. That is the partial-index comparison limit it has hit before, not a
    # real difference, and the same omission is recorded in a36bbe9e35d4. The
    # index stays.

    op.add_column(
        "instrument",
        sa.Column(
            "listing",
            sa.Enum(
                "KOSPI", "KOSDAQ", "NYSE", "NASDAQ", name="listing", native_enum=False, length=8
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "instrument",
        sa.Column("tracked", sa.Boolean(), server_default="false", nullable=False),
    )

    # 지금 있는 행은 시드된 워치리스트이고, 전부 가격과 공시를 받아 저장된
    # 동종군의 재료가 됐다. false로 두면 다음 실행에서 유니버스가 비고 점수가
    # 조용히 전부 바뀐다.
    #
    # **조건이 붙는 이유.** 무조건 UPDATE이면, 마스터로 3,950개 이름을 넣은 뒤
    # 누군가 downgrade → upgrade를 한 번만 해도 그 이름들이 전부 추적 대상이
    # 된다. 동종군이 9개에서 수천 개로 바뀌는 것 — 이 마이그레이션이 막겠다고
    # 쓴 바로 그 일이 마이그레이션 자신 때문에 일어난다. 캔들이 있다는 것이
    # 곧 "가격을 받아 채점할 수 있다"이므로 그것을 조건으로 쓴다.
    op.execute(
        "UPDATE instrument SET tracked = true "
        "WHERE instrument_id IN (SELECT DISTINCT instrument_id FROM candle)"
    )


def downgrade() -> None:
    op.drop_column("instrument", "tracked")
    op.drop_column("instrument", "listing")
    op.drop_index("ix_news_mention_instrument_item", table_name="news_mention")
    op.drop_table("news_mention")
    op.drop_index("ix_news_item_available", table_name="news_item")
    op.drop_table("news_item")

    # **`api_call_bucket`은 일부러 남긴다.** 이 테이블은 시장 데이터가 아니라
    # "한도를 넘지 않았다"는 주장의 유일한 근거다. 지우면 다음 호출은 예산이
    # 가득 찬 상태에서 시작하는데, 제공자 쪽 카운터는 그대로다. 즉 되돌리기
    # 한 번이 한도 초과를 가능하게 만든다 — 스키마를 되돌리는 것과 아무 관련이
    # 없는 결과다. 실제로 검증 중에 한 번 일어났다.
    #
    # 대칭성을 깨는 대가는 `upgrade()`의 조건부 생성 한 줄이고, 그 쪽이 훨씬
    # 싸다. 정말로 비우려면 사람이 직접 DROP 하면 된다.
