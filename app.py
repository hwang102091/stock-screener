"""
실행: python app.py
접속: http://localhost:5000
"""
import json, os, requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

CACHE     = "cache.json"
GROQ_KEY  = os.getenv("GROQ_API_KEY")

def load_cache():
    if not os.path.exists(CACHE):
        return {"updated_at": None, "stocks": []}
    with open(CACHE, encoding="utf-8") as f:
        return json.load(f)

# ── 종목 데이터 API ──
@app.route("/api/stocks")
def stocks():
    market = request.args.get("market", "ALL")
    data   = load_cache()
    items  = data["stocks"]
    if market != "ALL":
        items = [s for s in items if s.get("market") == market]
    return jsonify({"updated_at": data.get("updated_at"), "stocks": items})

# ── AI 분석 API ──
@app.route("/api/analyze", methods=["POST"])
def analyze():
    s = request.json
    if not s:
        return jsonify({"error": "no data"}), 400

    prompt = f"""다음 종목에 대해 전문적인 투자 분석을 제공해주세요.

[종목 정보]
- 종목명: {s.get('name')} ({s.get('ticker')})
- 시장: {'한국(KOSPI/KOSDAQ)' if s.get('market')=='KR' else '미국(NYSE/NASDAQ)'}
- 섹터: {s.get('sector')}
- PER: {s.get('per')}배
- PBR: {s.get('pbr')}배
- ROE: {s.get('roe')}%
- 배당수익률: {s.get('div')}%
- 시가총액: {s.get('mcap')}억달러
- 종합점수: {s.get('score')}/100

아래 5가지 관점에서 각각 3~4문장으로 분석해주세요:
1. 어떤 기업인지 설명 (제품, 고객, 매출처 등)
2. 밸류에이션 평가 (PER/PBR 기준 고평가/저평가 여부)
3. 수익성 분석 (ROE 및 배당 관점)
4. 섹터 내 포지셔닝 및 경쟁력
5. 투자자가 주목해야 할 리스크 또는 기회 요인

분석은 객관적 수치 기반으로 작성하며, 투자 권유는 하지 않습니다."""

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "당신은 CFA 자격증을 보유한 시니어 애널리스트입니다. 재무지표를 바탕으로 구조적이고 전문적인 한국어 분석을 제공합니다."},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens":  1200,
                "temperature": 0.3,
            },
            timeout=30,
        )
        text = res.json()["choices"][0]["message"]["content"]
        return jsonify({"result": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)