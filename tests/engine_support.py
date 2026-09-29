"""엔진 대역을 만드는 시험용 도우미."""

from __future__ import annotations

from typing import Any


def named(engine_class: type[Any], name: str, **attrs: Any) -> Any:
    """이름과 능력은 Engine 의 클래스 변수다. 실물 엔진은 클래스로 갈리므로
    대역도 인스턴스에 덮어쓰지 않고 클래스를 만들어 가른다."""
    return type(f"{engine_class.__name__}_{name}", (engine_class,), {"name": name, **attrs})
