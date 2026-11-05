"""Заглушка OpenAI-совместимого LLM-сервера для сквозных тестов (без GPU)."""
import json
import re

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI()


@app.get("/v1/models")
def models():
    return {"data": [{"id": "radar"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    user = body["messages"][-1]["content"]
    user = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user)
    schema = (body.get("response_format") or {}).get("json_schema", {}).get("schema", {})
    if "Составь аналитическую записку" in user or "апиши сводку" in user:
        return {"choices": [{"message": {"content": "Главное: за период зафиксирован очаг негатива в Мурино (ЖК Виктория), "
                                                    "жители жалуются на остановку лифта. Реакции УК пока нет."}}]}
    m = re.search(r"текст: <<<(.*?)>>>", user, re.S)
    text = (m.group(1) if m else user).lower()
    our = any(w in text for w in ("виктори", "балтийский дом", "авиаторов", "этажи", "екатерининск"))
    neighbor = "лампо" in text or "возрождение" in text
    neg = any(w in text for w in ("лифт", "не убира", "помогите", "сколько можно", "нет воды"))
    prio = "P1" if our and neg and ("юрьевич" in text or "губернатор" in text) else "P2" if (our and neg) or (neighbor and neg) else "P3" if our or neighbor else "P4"
    analysis = {"relevant": our or neighbor, "relation": "our" if our else "neighbor" if neighbor else "none",
                "entities": [], "sentiment": -2 if neg else 0, "tone": "жалоба" if neg else "нейтрально",
                "topics": ["лифт"] if "лифт" in text else ["уборка"] if "убира" in text else ["прочее"],
                "addressees": ["губернатор ЛО"] if "юрьевич" in text else [], "author_type": "житель",
                "call_to_action": "пишите" in text, "hazard": False, "virality": "средняя", "priority": prio,
                "summary": f"[fake-LLM] {'наш объект' if our else 'сосед' if neighbor else 'не про нас'}: {text[:80]}",
                "why": "тест", "action": "ответить от имени УК", "reply_draft": "Здравствуйте! Разбираемся.",
                "incident_hint": "Мурино: лифт" if "лифт" in text else "Мурино: прочее"}
    out = {"action": "final", "analysis": analysis} if "action" in schema.get("properties", {}) else analysis
    return {"choices": [{"message": {"content": json.dumps(out, ensure_ascii=False)}}]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18000, log_level="warning")
