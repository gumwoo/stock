"""지수 대형주: 그 시장 전체 시가총액에서 차지하는 비중이 큰 종목. 표시·사후 분석 전용, 순수.

삼성전자·SK하이닉스처럼 지수의 절반 가까이를 차지하는 종목은 개별 뉴스보다 업황·시장 흐름의 영향이 크고, 지수 대비
초과수익으로 재면 자기 자신과 비교하는 셈이 된다. 그래서 화면에 따로 표시하고, 목록 성과를 볼 때 따로 뗀다.
목록 선정·채점·판단에는 쓰지 않는다(목록 가설 H1~H7의 표본을 바꾸지 않게).

기준 5%: 2026-09-28 장 마감 뒤 KOSPI 순위는 삼성전자 25.69%, SK하이닉스 20.91%, 3위 SK스퀘어 2.34%로 둘과 나머지 사이가
크게 벌어져 있다.
"""

from __future__ import annotations

HEAVYWEIGHT_MIN_WEIGHT = 5.0
# 순위표가 이보다 오래됐으면(수집이 계속 실패) 판정하지 않는다.
MAX_RANK_AGE_SESSIONS = 10


def is_heavyweight(weight_pct: float | None) -> bool:
    return weight_pct is not None and weight_pct >= HEAVYWEIGHT_MIN_WEIGHT
