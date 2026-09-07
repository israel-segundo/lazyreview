from __future__ import annotations

from fastapi import APIRouter

from lazyreview.agents import list_adapters

router = APIRouter(tags=["agents"])


@router.get("/agents")
def agents():
    result = []
    for adapter in list_adapters():
        available = adapter.available()
        models = []
        if available:
            try:
                models = [
                    {"id": m.id, "label": m.label, "efforts": m.efforts}
                    for m in adapter.list_models()
                ]
            except Exception as e:
                models = []
                err = str(e)
            else:
                err = None
        else:
            err = f"{adapter.binary} not found on PATH"
        result.append(
            {
                "name": adapter.name,
                "display_name": adapter.display_name,
                "binary": adapter.binary,
                "available": available,
                "efforts": adapter.effort_options(),
                "models": models,
                "error": err if not available else None,
            }
        )
    return {"agents": result}
