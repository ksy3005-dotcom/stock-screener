"""
수급 추세추종 스크리너 - 한국투자증권 오픈API
================================================
핵심 조건:
  1. 외국인 순매수 N일 연속
  2. 기관 순매수 N일 연속
  3. 수급 강도 (오늘 순매수 > 20일 평균 1.5배)
  4. 외국인 누적 보유량 우상향
  5. 주가 20일선 위

설치:
    pip install flask flask-cors requests

실행:
    python trend_screener_server.py

접속:
    http://localhost:5100
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import json
import os
from datetime import datetime, timedelta
import time

app = Flask(__name__)
CORS(app)

# ── 설정 ────────────────────────────────────────────────────
BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE = {"token": None, "expires": 0}

# ── 대표 종목 리스트 ──────────────────────────────────────────
KOSPI_TICKERS = [
    ("005930","삼성전자"), ("000660","SK하이닉스"), ("005380","현대차"),
    ("000270","기아"), ("035420","NAVER"), ("005490","POSCO홀딩스"),
    ("035720","카카오"), ("068270","셀트리온"), ("207940","삼성바이오"),
    ("373220","LG에너지솔루션"), ("012330","현대모비스"), ("051910","LG화학"),
    ("028260","삼성물산"), ("003550","LG"), ("096770","SK이노베이션"),
    ("032830","삼성생명"), ("259960","크래프톤"), ("316140","우리금융"),
    ("086790","하나금융"), ("105560","KB금융"), ("055550","신한지주"),
    ("017670","SK텔레콤"), ("030200","KT"), ("015760","한국전력"),
    ("034020","두산에너빌리티"), ("042660","한화오션"), ("329180","HD현대중공업"),
    ("009540","HD한국조선해양"), ("011200","HMM"), ("180640","한화에어로스페이스"),
    ("047810","한국항공우주"), ("003490","대한항공"), ("064350","현대로템"),
    ("012450","한화시스템"), ("079550","LIG넥스원"), ("010130","고려아연"),
    ("006400","삼성SDI"), ("009830","한화솔루션"), ("011790","SKC"),
    ("010950","S-Oil"), ("267250","HD현대"), ("042670","HD현대인프라코어"),
    ("088350","한화생명"), ("000810","삼성화재"), ("001450","현대해상"),
    ("316140","우리금융"), ("139480","이마트"), ("004990","롯데지주"),
    ("069960","현대백화점"), ("023530","롯데쇼핑"),
]

KOSDAQ_TICKERS = [
    ("247540","에코프로비엠"), ("086520","에코프로"), ("196170","알테오젠"),
    ("141080","리가켐바이오"), ("028300","HLB"), ("042700","한미반도체"),
    ("112610","씨에스윈드"), ("091990","셀트리온제약"), ("145020","휴젤"),
    ("214150","클래시스"), ("277810","레인보우로보틱스"), ("357780","솔브레인"),
    ("066970","엘앤에프"), ("039030","이오테크닉스"), ("041510","에스엠"),
    ("035900","JYP엔터"), ("122870","와이지엔터"), ("352820","하이브"),
    ("263750","펄어비스"), ("095340","ISC"), ("403870","HPSP"),
    ("211270","AP시스템"), ("053300","피에스케이"), ("240810","원익IPS"),
    ("036810","에프에스티"), ("101490","에스앤에스텍"), ("336370","솔루스첨단소재"),
]

# ── 토큰 관리 ────────────────────────────────────────────────
def get_token(app_key, app_secret):
    now = time.time()
    if TOKEN_CACHE["token"] and TOKEN_CACHE["expires"] > now + 60:
        return TOKEN_CACHE["token"]
    
    res = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret
    }, timeout=10)
    d = res.json()
    TOKEN_CACHE["token"] = d.get("access_token")
    TOKEN_CACHE["expires"] = now + d.get("expires_in", 86400)
    return TOKEN_CACHE["token"]

def kis_get(path, params, tr_id, app_key, app_secret, token):
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P"
    }
    res = requests.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=10)
    return res.json()

# ── 수급 데이터 ──────────────────────────────────────────────
def get_investor_data(ticker, market, app_key, app_secret, token):
    """투자자별 일별 매매현황 (최근 30일)"""
    try:
        d = kis_get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            {"fid_cond_mrkt_div_code": market, "fid_input_iscd": ticker},
            "FHKST01010900", app_key, app_secret, token
        )
        return d.get("output", [])
    except:
        return []

def get_price_data(ticker, market, app_key, app_secret, token):
    """현재가 및 기본 정보"""
    try:
        d = kis_get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            {"fid_cond_mrkt_div_code": market, "fid_input_iscd": ticker},
            "FHKST01010100", app_key, app_secret, token
        )
        o = d.get("output", {})
        return {
            "price": int(o.get("stck_prpr", 0)),
            "change_pct": float(o.get("prdy_ctrt", 0)),
            "volume": int(o.get("acml_vol", 0)),
            "ma20": float(o.get("d20_dsrt", 0)),  # 20일 이격도
        }
    except:
        return {"price": 0, "change_pct": 0, "volume": 0, "ma20": 0}

# ── 수급 추세 분석 ───────────────────────────────────────────
def analyze_supply(investor_data, price_data):
    """
    수급 추세추종 조건 분석
    Returns: dict with score and conditions
    """
    if not investor_data or len(investor_data) < 5:
        return None

    to_int = lambda v: int(str(v).replace(",","").replace("-","0") or 0) if v and str(v).strip() not in ["-",""] else 0

    # 최근 데이터 (index 0 = 당일)
    recent = investor_data[:20]

    inst_vals  = [to_int(d.get("orgn_ntby_qty", 0)) for d in recent]
    fore_vals  = [to_int(d.get("frgn_ntby_qty", 0)) for d in recent]
    indv_vals  = [to_int(d.get("indv_ntby_qty", 0)) for d in recent]
    fore_hold  = [to_int(d.get("frgn_hldn_qty", 0)) for d in recent]

    # ── 조건 1: 외국인 연속 순매수 일수 ──
    fore_consec = 0
    for v in fore_vals:
        if v > 0: fore_consec += 1
        else: break

    # ── 조건 2: 기관 연속 순매수 일수 ──
    inst_consec = 0
    for v in inst_vals:
        if v > 0: inst_consec += 1
        else: break

    # ── 조건 3: 수급 강도 (오늘 vs 20일 평균) ──
    fore_avg = sum(abs(v) for v in fore_vals[1:]) / max(len(fore_vals[1:]), 1)
    inst_avg = sum(abs(v) for v in inst_vals[1:]) / max(len(inst_vals[1:]), 1)
    fore_surge = (fore_vals[0] / fore_avg) if fore_avg > 0 and fore_vals[0] > 0 else 0
    inst_surge = (inst_vals[0] / inst_avg) if inst_avg > 0 and inst_vals[0] > 0 else 0

    # ── 조건 4: 외국인 누적 보유량 우상향 (최근 10일) ──
    fore_hold_trend = False
    if len(fore_hold) >= 10 and fore_hold[0] > 0:
        recent_holds = fore_hold[:10]
        increases = sum(1 for i in range(len(recent_holds)-1) if recent_holds[i] >= recent_holds[i+1])
        fore_hold_trend = increases >= 6  # 10일 중 6일 이상 증가

    # ── 조건 5: 주가 20일선 위 ──
    above_ma20 = price_data.get("ma20", 0) >= 100  # 이격도 100 이상 = 20일선 위

    # ── 점수 계산 ──
    score = 0
    if fore_consec >= 3: score += 30
    elif fore_consec >= 2: score += 20
    elif fore_consec >= 1: score += 10

    if inst_consec >= 3: score += 25
    elif inst_consec >= 2: score += 15
    elif inst_consec >= 1: score += 8

    if fore_surge >= 2.0: score += 20
    elif fore_surge >= 1.5: score += 12
    elif fore_surge > 1.0: score += 6

    if fore_hold_trend: score += 15
    if above_ma20: score += 10

    # 개인 매도 (역발상 신호)
    indv_sell = indv_vals[0] < 0 if indv_vals else False
    if indv_sell: score += 5

    conditions = {
        "fore_consec": fore_consec,
        "inst_consec": inst_consec,
        "fore_surge": round(fore_surge, 1),
        "inst_surge": round(inst_surge, 1),
        "fore_hold_trend": fore_hold_trend,
        "above_ma20": above_ma20,
        "indv_sell": indv_sell,
        "fore_today": fore_vals[0] if fore_vals else 0,
        "inst_today": inst_vals[0] if inst_vals else 0,
    }

    return {"score": min(100, score), "conditions": conditions}

# ── API 엔드포인트 ────────────────────────────────────────────
@app.route("/api/token", methods=["POST"])
def issue_token():
    data = request.json
    try:
        token = get_token(data["app_key"], data["app_secret"])
        return jsonify({"status": "ok", "token": token[:10] + "..."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/scan", methods=["POST"])
def scan():
    data = request.json
    app_key    = data.get("app_key")
    app_secret = data.get("app_secret")
    market     = data.get("market", "J")  # J=KOSPI, Q=KOSDAQ
    min_score  = data.get("min_score", 50)
    min_fore_consec = data.get("min_fore_consec", 1)
    min_inst_consec = data.get("min_inst_consec", 1)
    limit      = data.get("limit", 100)

    try:
        token = get_token(app_key, app_secret)
    except Exception as e:
        return jsonify({"status": "error", "message": f"토큰 오류: {str(e)}"}), 500

    tickers = KOSPI_TICKERS[:limit] if market == "J" else KOSDAQ_TICKERS[:limit]
    results = []

    for ticker, name in tickers:
        try:
            investor = get_investor_data(ticker, market, app_key, app_secret, token)
            price    = get_price_data(ticker, market, app_key, app_secret, token)
            analysis = analyze_supply(investor, price)

            if not analysis:
                continue

            c = analysis["conditions"]
            if (analysis["score"] >= min_score and
                c["fore_consec"] >= min_fore_consec and
                c["inst_consec"] >= min_inst_consec):
                results.append({
                    "ticker": ticker,
                    "name": name,
                    "market": "KOSPI" if market == "J" else "KOSDAQ",
                    "price": price["price"],
                    "change_pct": price["change_pct"],
                    "volume": price["volume"],
                    "score": analysis["score"],
                    **c
                })
            time.sleep(0.1)  # API 호출 간격
        except Exception as e:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"status": "ok", "count": len(results), "data": results})

@app.route("/api/stock/<ticker>", methods=["POST"])
def stock_detail(ticker):
    data = request.json
    app_key    = data.get("app_key")
    app_secret = data.get("app_secret")
    market     = data.get("market", "J")

    try:
        token    = get_token(app_key, app_secret)
        investor = get_investor_data(ticker, market, app_key, app_secret, token)
        price    = get_price_data(ticker, market, app_key, app_secret, token)
        analysis = analyze_supply(investor, price)

        # 히스토리 (최근 10일)
        history = []
        for d in investor[:10]:
            to_int = lambda v: int(str(v).replace(",","") or 0) if v and str(v).strip() not in ["-",""] else 0
            history.append({
                "date": d.get("stck_bsop_date",""),
                "inst": to_int(d.get("orgn_ntby_qty")),
                "fore": to_int(d.get("frgn_ntby_qty")),
                "indv": to_int(d.get("indv_ntby_qty")),
                "fore_hold": to_int(d.get("frgn_hldn_qty")),
            })

        return jsonify({
            "status": "ok",
            "ticker": ticker,
            "price": price,
            "analysis": analysis,
            "history": history
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/")
def index():
    return send_from_directory(".", "trend_screener_ui.html")

if __name__ == "__main__":
    print("=" * 50)
    print("  수급 추세추종 스크리너 서버")
    print("  http://localhost:5100")
    print("=" * 50)
    app.run(debug=True, port=5100)
