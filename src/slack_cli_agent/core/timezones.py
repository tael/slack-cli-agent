"""이 봇이 쓰는 시간대.

사람에게 보이는 시각은 전부 이 시간대로 적는다. 같은 정의가 파일마다 따로
있으면 한쪽만 고쳤을 때 화면의 시각이 서로 어긋나고, 그 어긋남은 로그를
나란히 놓고 보기 전에는 드러나지 않는다.
"""

from __future__ import annotations

from datetime import timedelta, timezone

KST = timezone(timedelta(hours=9))
