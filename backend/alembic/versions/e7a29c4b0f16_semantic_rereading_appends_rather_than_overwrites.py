"""재검증이 덮어쓰지 않고 쌓이도록 semantic_version을 유니크 키에 넣는다

`d4f1b7c2e893`이 각 팩트에 수집 당시의 reading을 기록했고, 재수집은 값이 같을 때 그
행의 `semantic_version`을 제자리에서 갱신했다. 싸 보였지만 시간축을 거슬렀다.

    2024년에 수집된 행      ingested_at = 2024, semantic_version = 1
    2026년에 재수집          값이 같으므로 semantic_version = 2 (ingested_at은 2024 그대로)
    2025년 snapshot으로 조회  v2로 보임   ← 2025년에는 존재하지 않던 정보

`ingested_at`으로만 자르는 조회는 그 행을 2025년에도 보게 되고, "이 행이 v2 의미로
검증됐다"는 2026년의 사실이 2025년 스냅샷에 소급된다. 값이 같더라도 검증됐다는 사실
자체가 새 정보다.

이 테이블은 값 정정을 이미 append-only로 다룬다 — 재작성은 덮어쓰기가 아니라 새 행이다.
새 reading으로 다시 읽는 것도 같은 종류이므로 같은 방식으로 다룬다. `semantic_version`을
유니크 키에 넣으면 재수집이 충돌하지 않고 자기 `ingested_at`을 단 새 행으로 쌓인다.

그래서 조회도 바뀐다. 옛 행이 새 행 옆에 살아남으므로 전체에 대한 단순 `MIN`은 영원히
1을 답한다. 팩트별로 가장 최근 reading을 구한 뒤 그중 최솟값을 보는 것이 맞고, 데이터셋은
가장 뒤처진 팩트가 현재일 때 현재다.

제약을 넓히는 것만으로는 부족하다. `d4f1b7c2e893` 이후에 재수집한 데이터베이스에는 이미
제자리로 찍힌 도장이 남아 있다 — 2026년에 정해진 reading을 주장하면서 `ingested_at`은
그보다 이른 행들이다. 그 도장들은 되돌린다. 값은 건드리지 않고 reading만 1로 되돌리므로,
다음 재수집이 제대로 된 `ingested_at`을 단 v2 행을 옆에 쌓는다.

    python -m app.cli collect --source dart --period max

그때까지 재무를 읽는 런은 거부된다. 그게 맞는 동작이다. 이 데이터베이스는 아직 현재
reading으로 검증된 적이 없다.

Revision ID: e7a29c4b0f16
Revises: d4f1b7c2e893
"""

from __future__ import annotations

from alembic import op

revision = "e7a29c4b0f16"
down_revision = "d4f1b7c2e893"
branch_labels = None
depends_on = None

CONSTRAINT = "uq_fundamental_context_filing"
COLUMNS = [
    "instrument_id",
    "taxonomy",
    "concept",
    "unit",
    "period_start",
    "period_end",
    "form",
    "filed_at",
    "accession",
]


def upgrade() -> None:
    # 제자리 도장 되돌리기. 어느 행이 그렇게 찍혔는지 구분할 방법이 없으므로 전부
    # 되돌린다. 과소평가하는 방향이라 안전하다 — 최악의 경우 재수집을 한 번 더
    # 요구할 뿐, 검증되지 않은 데이터셋을 검증됐다고 말하지 않는다.
    op.execute("UPDATE fundamental SET semantic_version = 1 WHERE source = 'DART'")

    op.drop_constraint(CONSTRAINT, "fundamental", type_="unique")
    op.create_unique_constraint(
        CONSTRAINT,
        "fundamental",
        [*COLUMNS, "semantic_version"],
        # `period_start`가 NULL인 순간 팩트 — 잔액처럼 기간이 아니라 시점에
        # 측정되는 값 — 때문에 필수다. Postgres 기본값은 모든 NULL을 서로
        # 다르다고 보므로, 이것이 없으면 Assets·StockholdersEquity·Cash가
        # 절대 충돌하지 않고 수집할 때마다 중복된다.
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "fundamental", type_="unique")
    op.create_unique_constraint(
        CONSTRAINT,
        "fundamental",
        COLUMNS,
        postgresql_nulls_not_distinct=True,
    )
