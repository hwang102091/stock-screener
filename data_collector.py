"""
실행 순서:
  1. python build_mapping.py   (최초 1회)
  2. python data_collector.py  (매일 야간 배치, 약 35분)
결과: cache.json 갱신
"""

import os, json, time, re
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
DART_KEY = os.getenv("DART_API_KEY")
CACHE    = "cache.json"
MAPPING  = "mapping.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ────────────────────────────────────────────
# 0. yfinance 설치 확인
# ────────────────────────────────────────────
try:
    import yfinance as yf
except ImportError:
    import subprocess, sys
    print("yfinance 설치 중...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance"])
    import yfinance as yf

# ────────────────────────────────────────────
# 0. 매핑 테이블 로드
# ────────────────────────────────────────────

def load_mapping() -> dict:
    if not os.path.exists(MAPPING):
        raise FileNotFoundError("mapping.json 없음 → 먼저 python build_mapping.py 실행")
    with open(MAPPING, encoding="utf-8") as f:
        data = json.load(f)
    print(f"매핑 로드: {data['count']}개 종목 ({data['built_at'][:10]} 기준)")
    return data["mapping"]


# ────────────────────────────────────────────
# 1. 국내 — 네이버 금융 시가총액 순위
#    시총 상위 200개 수집
# ────────────────────────────────────────────

def fetch_naver_stock_page(sosok: str, page: int) -> list:
    """네이버 금융 시가총액 순위 한 페이지 파싱"""
    headers = {"User-Agent": UA, "Referer": "https://finance.naver.com/"}
    r = requests.get(
        "https://finance.naver.com/sise/sise_market_sum.naver",
        params={"sosok": sosok, "page": page},
        headers=headers, timeout=10,
    )
    r.encoding = "EUC-KR"

    rows = re.findall(
        r'code=(\d{6}).*?'
        r'<td[^>]*class="number"[^>]*>([\d,]+)</td>'  # 현재가
        r'.*?<td[^>]*class="number"[^>]*>[\d,]+</td>' # 전일비
        r'.*?<td[^>]*class="number"[^>]*>[\d,.]+</td>'# 등락률
        r'.*?<td[^>]*class="number"[^>]*>[\d,]+</td>' # 거래량
        r'.*?<td[^>]*class="number"[^>]*>([\d,]+)</td>'  # 시가총액(억)
        r'.*?<td[^>]*class="number"[^>]*>([\d,.]+|-)</td>'  # PER
        r'.*?<td[^>]*class="number"[^>]*>([\d,.]+|-)</td>', # PBR
        r.text, re.DOTALL
    )
    result = []
    for row in rows:
        try:
            result.append({
                "ticker": row[0],
                "price":  float(row[1].replace(",", "") or 0),
                "mcap":   float(row[2].replace(",", "") or 0) / 1e4,
                "per":    float(row[3].replace(",", "") or 0) if row[3] != "-" else 0.0,
                "pbr":    float(row[4].replace(",", "") or 0) if row[4] != "-" else 0.0,
            })
        except Exception:
            continue
    return result

def fetch_kr_stocks(top_n: int = 200) -> list:
    """KOSPI + KOSDAQ 시총 상위 top_n개 수집"""
    all_stocks = []
    seen = set()

    for sosok, mkt_name in [("0", "KOSPI"), ("1", "KOSDAQ")]:
        page = 1
        while True:
            try:
                rows = fetch_naver_stock_page(sosok, page)
                if not rows:
                    break
                for row in rows:
                    if row["ticker"] not in seen:
                        row["market"] = "KR"
                        all_stocks.append(row)
                        seen.add(row["ticker"])
                page += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"  네이버 {mkt_name} p{page} 오류: {e}")
                break

    # 시총 기준 상위 top_n개 선별
    all_stocks.sort(key=lambda x: x["mcap"], reverse=True)
    return all_stocks[:top_n]


# ────────────────────────────────────────────
# 2. 국내 — DART ROE 조회
# ────────────────────────────────────────────

ROE_NAMES    = ["자기자본이익률", "자기자본순이익률", "ROE", "Return on Equity"]
NI_NAMES     = ["당기순이익", "연결당기순이익", "당기순이익(손실)", "당기순손익"]
EQUITY_NAMES = ["자본총계", "자기자본", "자본합계", "연결자본총계"]

def get_amount(items, names):
    for nm in names:
        item = next((i for i in items if nm in i.get("account_nm", "")), None)
        if item:
            try:
                return float(item["thstrm_amount"].replace(",", ""))
            except Exception:
                pass
    return None

def fetch_dart_roe(corp_code: str, year: str) -> float | None:
    for fs_div in ["CFS", "OFS"]:
        try:
            r = requests.get(
                "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                params={
                    "crtfc_key": DART_KEY, "corp_code": corp_code,
                    "bsns_year": year, "reprt_code": "11011", "fs_div": fs_div,
                },
                timeout=10,
            )
            items = r.json().get("list", [])
            if not items:
                continue
            for nm in ROE_NAMES:
                item = next((i for i in items if nm in i.get("account_nm", "")), None)
                if item:
                    try:
                        return round(float(item["thstrm_amount"].replace(",", "")), 1)
                    except Exception:
                        pass
            ni = get_amount(items, NI_NAMES)
            eq = get_amount(items, EQUITY_NAMES)
            if ni is not None and eq and eq != 0:
                return round(ni / eq * 100, 1)
        except Exception:
            pass
    return None

def enrich_kr_stocks(stocks: list, mapping: dict) -> list:
    """종목명·섹터·ROE를 mapping + DART에서 채워넣기"""
    year = str(datetime.today().year - 1)
    total = len(stocks)
    for i, s in enumerate(stocks):
        ticker = s["ticker"]
        info   = mapping.get(ticker, {})
        s["name"]   = info.get("corp_name", ticker)
        s["sector"] = info.get("sector", "기타")
        s["div"]    = 0.0

        corp_code = info.get("corp_code", "")
        if not corp_code:
            print(f"  [{i+1}/{total}] {ticker}: corp_code 없음, 건너뜀")
            s["roe"] = None
            continue

        print(f"  [{i+1}/{total}] {s['name']} ({ticker}) ROE 조회 중...")
        s["roe"] = fetch_dart_roe(corp_code, year)
        time.sleep(0.25)
    return stocks


# ────────────────────────────────────────────
# 3. 미국 — yfinance S&P 500 전체
# ────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    """S&P 500 전체 종목 목록"""
    return [
        # 기술
        "AAPL","MSFT","NVDA","GOOGL","GOOG","META","AVGO","ORCL","CRM","ADBE",
        "AMD","INTC","QCOM","TXN","MU","AMAT","LRCX","KLAC","MRVL","SNPS",
        "CDNS","ANSS","FTNT","PANW","CRWD","OKTA","ZS","DDOG","NET","SNOW",
        "NOW","TEAM","WDAY","VEEV","HUBS","ZM","DOCU","BOX","ROP","CTSH",
        "ACN","IBM","HPQ","HPE","DELL","NTAP","WDC","STX","KEYS","JNPR",
        "CSCO","ANET","FFIV","AKAM","CDW","LDOS","SAIC","BAH","EPAM","GLOB",
        "MSCI","MCO","SPGI","ICE","CME","NDAQ","CBOE","FDS","BR","VRSK",
        # 소비재
        "AMZN","TSLA","HD","MCD","NKE","SBUX","LOW","TGT","COST","WMT",
        "TJX","ROST","BURL","DG","DLTR","KR","SYY","YUM","CMG","DRI",
        "F","GM","APTV","BWA","LEA","MGA","NCLH","CCL","RCL","MAR",
        "HLT","IHG","H","CHH","WH","MGM","LVS","WYNN","CZR","PENN",
        "NFLX","DIS","PARA","WBD","FOX","FOXA","NYT","OMC","IPG","PUB",
        "AMCX","VIAC","LYV","CUK","EXPE","BKNG","ABNB","LYFT","UBER","DASH",
        "EBAY","ETSY","W","CHWY","CPNG","MELI","SE","GRAB","GLBE","SHOP",
        # 금융
        "BRK-B","JPM","V","MA","BAC","WFC","GS","MS","C","AXP",
        "BLK","SCHW","CB","MMC","AON","AJG","WTW","MET","PRU","AFL",
        "TRV","ALL","PGR","HIG","CNA","L","RNR","RE","EG","CINF",
        "USB","PNC","TFC","COF","DFS","SYF","ALLY","RF","CFG","FITB",
        "HBAN","KEY","MTB","ZION","CMA","FHN","SNV","PBCT","TCF","VLY",
        "GPN","FIS","FISV","PYPL","SQ","AFRM","SOFI","OPFI","HOOD","COIN",
        "ICE","CME","NDAQ","CBOE","MKTX","VIRT","IBKR","LPLA","RJF","SF",
        "AMG","BEN","IVZ","TROW","BX","KKR","APO","CG","ARES","BAM",
        # 헬스케어
        "JNJ","UNH","LLY","ABBV","MRK","TMO","ABT","DHR","BMY","AMGN",
        "GILD","ISRG","SYK","BSX","MDT","ZTS","REGN","VRTX","BIIB","MRNA",
        "BDX","EW","BAX","HOLX","IDXX","IQV","A","WAT","PKI","RMD",
        "HUM","CI","ELV","MOH","CNC","CVS","WBA","MCK","ABC","CAH",
        "INCY","ALNY","SGEN","BMRN","EXEL","ACAD","NBIX","PTGX","KURA","FATE",
        "DXCM","PHG","STE","HAE","TFX","NVCR","SWAV","NTRA","VEEV","DOCS",
        # 산업재
        "GE","CAT","BA","HON","RTX","LMT","NOC","DE","MMM","EMR",
        "ETN","PH","ROK","DOV","XYL","ITW","GD","HII","TDG","HEI",
        "CARR","OTIS","TT","JCI","AOS","CMI","PCAR","AGCO","WM","RSG",
        "UPS","FDX","CSX","UNP","NSC","CP","CNI","R","CHRW","EXPD",
        "GWW","MSC","FAST","NDSN","RRX","GNRC","FBIN","ALLE","AYI","HUBB",
        "PWR","MTZ","DY","MYR","WLDN","STRL","APOG","TREX","MAS","LII",
        "URI","HEES","H","GATX","AL","AER","TDY","HWM","ARNC","ATI",
        # 에너지
        "XOM","CVX","COP","EOG","SLB","MPC","PSX","VLO","OXY","PXD",
        "HAL","BKR","DVN","FANG","HES","MRO","APA","EQT","RRC","COG",
        "KMI","WMB","OKE","ET","EPD","MPLX","PAA","TRGP","DT","LNG",
        "BP","SHEL","TTE","E","ENB","SU","CVE","IMO","HSE","MEG",
        # 유틸리티
        "NEE","DUK","SO","D","AEP","EXC","SRE","PCG","ED","FE",
        "ES","WEC","DTE","ETR","PPL","AEE","CMS","NI","PNW","OGE",
        "AWK","SJW","MSEX","YORW","ARTNA","GWRS","EGAN","UTMD","CWCO","PESI",
        # 필수소비재
        "PG","KO","PEP","PM","MO","MDLZ","CL","KMB","CHD","CLX",
        "GIS","K","CPB","CAG","SJM","MKC","HRL","TSN","STZ","BF-B",
        "KHC","TAP","SAM","MNST","KDP","COKE","PBH","SPB","CENT","SENEA",
        "WBA","CVS","RAD","WOOF","PETQ","CHWY","FRPT","BYND","APPH","OATS",
        # 소재
        "LIN","APD","ECL","SHW","PPG","NEM","FCX","NUE","STLD","RS",
        "VMC","MLM","CRH","EXP","SUM","BLL","AMCR","PKG","SEE","SON",
        "IP","WRK","GPK","SLGN","BERY","ATR","PTVE","SWIM","HDSN","ASIX",
        "ALB","MP","LTHM","SQM","VALE","RIO","BHP","SCCO","TCK","FM",
        # 부동산
        "AMT","PLD","CCI","EQIX","PSA","DLR","O","WPC","NNN","VICI",
        "SPG","MAC","KIM","REG","BXP","SLG","VNO","HPP","CUZ","PDM",
        "EXR","CUBE","LSI","NSA","SSS","REXR","FR","EGP","STAG","LXP",
        # 나스닥 추가 우량주 (S&P 500 미포함)
        "TSM","ASML","ARM","MELI","PDD","BIDU","JD","NTES","BABA","TCEHY",
        "SMSN","005930","SONY","TM","HMC","RACE","FCAU","STLA","NIO","XPEV",
        "CELH","RXRX","ROIV","IOVA","BEAM","EDIT","NTLA","CRSP","VERV","GRPH",
        "SOUN","BBAI","PLTR","RKLB","ASTS","LUNR","RDW","SPCE","ASTR","MNTS",
        "CAVA","BROS","SHAK","WING","DNUT","FAT","JACK","LOCO","NATH","TXRH",
        "FOUR","GH","ACMR","AEHR","ALGM","AMBA","AXTI","COHU","DIOD","EGAN",
    ]

def safe_float(val, mult=1.0, default=0.0) -> float:
    try:
        v = float(val)
        return round(v * mult, 2)
    except Exception:
        return default

def fetch_yf_stock(ticker: str) -> dict | None:
    """yfinance로 단일 종목 재무지표 수집"""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        if not info or info.get("regularMarketPrice") is None:
            return None
        return {
            "name":   info.get("longName") or info.get("shortName", ticker),
            "ticker": ticker,
            "market": "US",
            "sector": info.get("sector", ""),
            "per":    safe_float(info.get("trailingPE")),
            "pbr":    safe_float(info.get("priceToBook")),
            "roe":    safe_float(info.get("returnOnEquity"), mult=100),
            "div":    safe_float(info.get("dividendYield"), mult=100),
            "mcap":   safe_float(info.get("marketCap"), mult=1/1e8),
            "price":  safe_float(info.get("regularMarketPrice")),
        }
    except Exception as e:
        print(f"    yfinance 오류 ({ticker}): {e}")
        return None

def fetch_us_stocks() -> list:
    tickers = get_sp500_tickers()
    total   = len(tickers)
    result  = []
    fail    = 0

    for i, ticker in enumerate(tickers):
        print(f"  [{i+1}/{total}] {ticker} 수집 중...")
        data = fetch_yf_stock(ticker)
        if data:
            result.append(data)
        else:
            fail += 1
        time.sleep(0.5)   # yfinance는 rate limit 여유로움

    print(f"  → 성공: {len(result)}개 / 실패: {fail}개")
    return result


# ────────────────────────────────────────────
# 4. 종합점수 계산
# ────────────────────────────────────────────

def calc_score(s: dict) -> int:
    score = 50
    per  = s.get("per")  or 0
    pbr  = s.get("pbr")  or 0
    roe  = s.get("roe")  or 0
    div  = s.get("div")  or 0
    mcap = s.get("mcap") or 0

    # ── PER 평가 (성장성 고려) ──
    # ROE가 높으면 높은 PER을 일부 용인 (PEG 개념)
    peg = (per / roe) if (roe > 0 and per > 0) else 99
    if peg < 0.5:       score += 15   # 극저평가 성장주
    elif peg < 1.0:     score += 10   # 저평가 성장주
    elif peg < 1.5:     score += 5    # 적정
    elif peg < 2.5:     score += 0    # 다소 고평가
    else:               score -= 8    # 고평가

    # PER 절대값 보정
    if 0 < per <= 10:   score += 5    # 매우 저PER 추가 가점
    elif per > 60:      score -= 5    # 극고PER 추가 감점
    elif per == 0:      score -= 5    # PER 없음 (적자)

    # ── PBR 평가 ──
    if 0 < pbr <= 1:    score += 12
    elif pbr <= 2:      score += 7
    elif pbr <= 4:      score += 3
    elif pbr <= 8:      score += 0
    elif pbr > 8:       score -= 6

    # ── ROE 평가 (수익성) ──
    if roe >= 30:       score += 15
    elif roe >= 20:     score += 12
    elif roe >= 15:     score += 8
    elif roe >= 8:      score += 4
    elif roe >= 3:      score += 0
    elif roe < 0:       score -= 12   # 적자
    else:               score -= 5    # 저수익

    # ── 배당 평가 ──
    if div >= 5:        score += 6
    elif div >= 3:      score += 4
    elif div >= 1.5:    score += 2

    # ── 시총 규모 (안정성) ──
    mkt = s.get("market", "US")
    if mkt == "KR":
        if mcap >= 10:    score += 3   # 10조 이상 대형주
        elif mcap >= 1:   score += 1
    else:
        if mcap >= 5000:  score += 3   # 5000억달러 이상
        elif mcap >= 500: score += 1

    return max(0, min(100, score))


# ────────────────────────────────────────────
# 5. 메인
# ────────────────────────────────────────────

def main():
    print("=== 데이터 수집 시작 ===\n")
    mapping    = load_mapping()
    all_stocks = []

    # ── 국내 ──
    print("\n[1/3] 네이버 금융 국내 종목 수집 (시총 상위 500개)...")
    kr_stocks = fetch_kr_stocks(top_n=500)
    print(f"  → {len(kr_stocks)}개 수집")

    print("\n[2/3] DART ROE + 섹터 보강 (약 15분)...")
    kr_stocks = enrich_kr_stocks(kr_stocks, mapping)
    for s in kr_stocks:
        s["score"] = calc_score(s)
    all_stocks.extend(kr_stocks)

    # ── 미국 ──
    print("\n[3/3] yfinance S&P 500 전체 수집 (약 10~20분)...")
    us_stocks = fetch_us_stocks()
    for s in us_stocks:
        s["score"] = calc_score(s)
    all_stocks.extend(us_stocks)

    # ── 저장 ──
    cache = {
        "updated_at": datetime.now().isoformat(),
        "stocks":     all_stocks,
    }
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    # ── HTML 생성 ──
    print("\nHTML 생성 중...")
    generate_html(all_stocks, cache["updated_at"])
    print("  screener.html 저장 완료")

def generate_html(stocks: list, updated_at: str):
    stocks_json = json.dumps(stocks, ensure_ascii=False)
    updated_str = updated_at[:16].replace("T", " ")
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>주식 스크리너</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8f9fa; color: #212529; font-size: 14px; }}
  .header {{ background: #fff; border-bottom: 1px solid #e9ecef; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 101; }}
  .header h1 {{ font-size: 16px; font-weight: 600; }}
  .updated {{ font-size: 11px; color: #868e96; }}
  .controls {{ background: #fff; padding: 12px 16px; border-bottom: 1px solid #e9ecef; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; position: sticky; top: 45px; z-index: 100; }}
  .tabs {{ display: flex; gap: 6px; }}
  .tab {{ padding: 5px 14px; border-radius: 20px; border: 1px solid #dee2e6; background: #fff; cursor: pointer; font-size: 13px; }}
  .tab.active {{ background: #228be6; color: #fff; border-color: #228be6; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
  .filter-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: #495057; }}
  .filter-item input[type=range] {{ width: 90px; }}
  .filter-val {{ min-width: 36px; font-weight: 500; color: #212529; }}
  .search {{ padding: 5px 10px; border: 1px solid #dee2e6; border-radius: 6px; font-size: 13px; width: 140px; }}
  .count {{ font-size: 12px; color: #868e96; margin-left: auto; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; }}
  th {{ padding: 8px 10px; text-align: left; font-size: 12px; font-weight: 500; color: #868e96; border-bottom: 2px solid #e9ecef; cursor: pointer; white-space: nowrap; user-select: none; }}
  th:hover {{ color: #212529; }}
  th.num {{ text-align: right; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #f1f3f5; white-space: nowrap; }}
  td.num {{ text-align: right; }}
  tr:hover td {{ background: #f8f9fa; }}
  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 500; }}
  .badge-kr {{ background: #e7f5ff; color: #1971c2; }}
  .badge-us {{ background: #ebfbee; color: #2f9e44; }}
  .score-wrap {{ display: flex; align-items: center; gap: 6px; }}
  .score-bar {{ flex: 1; height: 5px; background: #e9ecef; border-radius: 3px; min-width: 50px; }}
  .score-fill {{ height: 100%; border-radius: 3px; }}
  .score-num {{ font-size: 12px; font-weight: 500; min-width: 24px; text-align: right; }}
  .ai-btn {{ padding: 3px 8px; font-size: 11px; border: 1px solid #dee2e6; border-radius: 4px; background: #fff; color: #228be6; cursor: pointer; }}
  .ai-btn:hover {{ background: #e7f5ff; }}
  .ai-panel {{ background: #fff; border-top: 1px solid #e9ecef; padding: 16px; }}
  .ai-panel h3 {{ font-size: 14px; font-weight: 600; margin-bottom: 8px; }}
  .ai-metrics {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }}
  .metric {{ background: #f8f9fa; border-radius: 6px; padding: 6px 12px; text-align: center; }}
  .metric-label {{ font-size: 11px; color: #868e96; }}
  .metric-val {{ font-size: 15px; font-weight: 600; }}
  .ai-text {{ font-size: 13px; line-height: 1.8; color: #495057; white-space: pre-wrap; }}
  .ai-loading {{ color: #868e96; font-size: 13px; }}
  .empty {{ text-align: center; padding: 40px; color: #868e96; }}
  @media (max-width: 600px) {{
    .filters {{ gap: 8px; }}
    .filter-item input[type=range] {{ width: 70px; }}
    .search {{ width: 110px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <h1>📊 주식 스크리너</h1>
  <span class="updated">업데이트: {updated_str}</span>
</div>

<div class="controls">
  <div class="tabs">
    <button class="tab active" onclick="setTab('ALL')">전체</button>
    <button class="tab" onclick="setTab('KR')">국내</button>
    <button class="tab" onclick="setTab('US')">미국</button>
  </div>
  <div class="filters">
    <div class="filter-item">PER ≤ <input type="range" id="per" min="0" max="100" value="100" oninput="render()"><span class="filter-val" id="per-v">100</span></div>
    <div class="filter-item">PBR ≤ <input type="range" id="pbr" min="0" max="30" value="30" step="0.5" oninput="render()"><span class="filter-val" id="pbr-v">30</span></div>
    <div class="filter-item">ROE ≥ <input type="range" id="roe" min="0" max="50" value="0" oninput="render()"><span class="filter-val" id="roe-v">0</span>%</div>
    <div class="filter-item">배당 ≥ <input type="range" id="div" min="0" max="10" value="0" step="0.5" oninput="render()"><span class="filter-val" id="div-v">0</span>%</div>
    <input class="search" type="text" id="search" placeholder="종목명 검색" oninput="render()">
  </div>
  <span class="count" id="count"></span>
</div>

<div class="table-wrap">
<table>
  <thead>
    <tr>
      <th onclick="sort('name')">종목 <span id="s-name"></span></th>
      <th class="num" onclick="sort('per')" title="주가수익비율 = 주가 ÷ 주당순이익. 낮을수록 저평가. 단, 성장주는 높을 수 있음">PER ⓘ <span id="s-per"></span></th>
      <th class="num" onclick="sort('pbr')" title="주가순자산비율 = 주가 ÷ 주당순자산. 1배 미만이면 자산 대비 저평가">PBR ⓘ <span id="s-pbr"></span></th>
      <th class="num" onclick="sort('roe')" title="자기자본이익률 = 순이익 ÷ 자기자본. 높을수록 자본 효율이 좋은 기업. 15% 이상이면 우량">ROE ⓘ <span id="s-roe"></span></th>
      <th class="num" onclick="sort('div')" title="배당수익률 = 주당배당금 ÷ 주가. 높을수록 현금 수익 기대 가능. 단, 지속가능성 확인 필요">배당% ⓘ <span id="s-div"></span></th>
      <th class="num" onclick="sort('mcap')">시총 <span id="s-mcap"></span></th>
      <th onclick="sort('mcap')" style="min-width:100px" title="PER/PBR/ROE/배당/시총 등을 종합한 밸류에이션 점수. PEG(PER÷ROE) 기반으로 성장주도 고려. 85↑우량 / 70↑양호 / 이하 주의">점수 ⓘ <span id="s-score"></span></th>
      <th>AI분석</th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>
</div>
<div id="ai-panel" class="ai-panel" style="display:none"></div>

<script>
const ALL = {stocks_json};
let tab = 'ALL', sortKey = 'score', sortDir = -1, selTicker = null;

function fv(id) {{ return parseFloat(document.getElementById(id).value); }}

function filtered() {{
  const per = fv('per'), pbr = fv('pbr'), roe = fv('roe'), div = fv('div');
  const q = document.getElementById('search').value.toLowerCase();
  document.getElementById('per-v').textContent = per;
  document.getElementById('pbr-v').textContent = pbr;
  document.getElementById('roe-v').textContent = roe;
  document.getElementById('div-v').textContent = div;
  return ALL.filter(s =>
    (tab === 'ALL' || s.market === tab) &&
    (s.per === 0 || s.per <= per) &&
    (s.pbr === 0 || s.pbr <= pbr) &&
    ((s.roe || 0) >= roe) &&
    ((s.div || 0) >= div) &&
    (s.name || s.ticker).toLowerCase().includes(q)
  ).sort((a, b) => {{
    const av = a[sortKey] ?? (sortDir > 0 ? Infinity : -Infinity);
    const bv = b[sortKey] ?? (sortDir > 0 ? Infinity : -Infinity);
    return sortDir * (bv - av);
  }});
}}

function scoreColor(s) {{
  return s >= 85 ? '#2f9e44' : s >= 70 ? '#e67700' : '#c92a2a';
}}

function fmt(v, d=1) {{ return v ? v.toFixed(d) : '-'; }}
function fmtMcap(v, mkt) {{
  if (!v) return '-';
  if (mkt === 'KR') return v >= 1 ? v.toFixed(1) + '조' : (v * 1000).toFixed(0) + '억';
  return v >= 10000 ? (v/10000).toFixed(1) + 'T' : v.toFixed(0) + 'B';
}}

function render() {{
  const rows = filtered();
  document.getElementById('count').textContent = rows.length + '개 종목';
  const tbody = document.getElementById('tbody');
  if (!rows.length) {{ tbody.innerHTML = '<tr><td colspan="8" class="empty">조건에 맞는 종목이 없습니다</td></tr>'; return; }}
  tbody.innerHTML = rows.map(s => {{
    const sc = s.score || 0;
    const col = scoreColor(sc);
    const badge = s.market === 'KR'
      ? '<span class="badge badge-kr">KR</span>'
      : '<span class="badge badge-us">US</span>';
    const sel = s.ticker === selTicker ? 'background:#e7f5ff' : '';
    return `<tr style="${{sel}}">
      <td>${{badge}} ${{s.name || s.ticker}}<br><span style="font-size:11px;color:#868e96">${{s.sector || ''}}</span></td>
      <td class="num">${{fmt(s.per)}}</td>
      <td class="num">${{fmt(s.pbr)}}</td>
      <td class="num" style="color:${{(s.roe||0)>=20?'#2f9e44':'inherit'}}">${{fmt(s.roe)}}%</td>
      <td class="num" style="color:${{(s.div||0)>=3?'#2f9e44':'inherit'}}">${{fmt(s.div)}}%</td>
      <td class="num" style="font-size:12px;color:#868e96">${{fmtMcap(s.mcap, s.market)}}</td>
      <td>
        <div class="score-wrap">
          <div class="score-bar"><div class="score-fill" style="width:${{sc}}%;background:${{col}}"></div></div>
          <span class="score-num" style="color:${{col}}">${{sc}}</span>
        </div>
      </td>
      <td><button class="ai-btn" onclick="analyze('${{s.ticker}}')">분석 ↗</button></td>
    </tr>`;
  }}).join('');
}}

function sort(key) {{
  if (sortKey === key) sortDir *= -1;
  else {{ sortKey = key; sortDir = -1; }}
  ['name','per','pbr','roe','div','mcap','score'].forEach(k => {{
    document.getElementById('s-' + k).textContent = k === sortKey ? (sortDir < 0 ? '↓' : '↑') : '';
  }});
  render();
}}

function setTab(t) {{
  tab = t;
  document.querySelectorAll('.tab').forEach((el, i) => {{
    el.classList.toggle('active', ['ALL','KR','US'][i] === t);
  }});
  render();
}}

async function analyze(ticker) {{
  const s = ALL.find(x => x.ticker === ticker);
  if (!s) return;

  // 이미 열려있으면 닫기
  if (selTicker === ticker) {{
    selTicker = null;
    render();
    return;
  }}

  selTicker = ticker;
  render();

const RENDER_URL = 'https://stock-screener-api.onrender.com';

  const aiTextEl = document.getElementById(`ai-text-${{ticker}}`);
  try {{
    const res = await fetch(`${{RENDER_URL}}/api/analyze`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(s)
    }});
    const data = await res.json();
    const text = data.result || data.error || '분석 결과를 가져올 수 없습니다.';
    aiTextEl.textContent = text;
  }} catch(e) {{
    aiTextEl.textContent = '분석 오류: ' + e.message;
  }}
}}

function fetchChart(ticker, canvasId) {{
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const divId = canvasId + '-tv';
  canvas.outerHTML = `<div id="${{divId}}" style="width:100%;height:300px"></div>`;

  let tvSymbol;
  if (ticker.endsWith('.KS')) {{
    tvSymbol = 'KRX:' + ticker.replace('.KS', '');
  }} else if (ticker.endsWith('.KQ')) {{
    tvSymbol = 'KOSDAQ:' + ticker.replace('.KQ', '');
  }} else {{
    tvSymbol = ticker;
  }}

  const container = document.getElementById(divId);
  if (!container) return;

  container.innerHTML = `
    <div class="tradingview-widget-container" style="height:300px">
      <div id="tv-chart-${{divId}}" style="height:260px"></div>
      <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js"><\/script>
      <script type="text/javascript">
        new TradingView.widget({{
          container_id: "tv-chart-${{divId}}",
          symbol: "${{tvSymbol}}",
          interval: "D",
          timezone: "Asia/Seoul",
          theme: "light",
          style: "1",
          locale: "kr",
          toolbar_bg: "#f1f3f6",
          enable_publishing: false,
          hide_top_toolbar: false,
          hide_legend: false,
          save_image: false,
          height: 260,
          width: "100%",
          range: "3M",
          allow_symbol_change: false,
        }});
      <\/script>
    </div>`;
}}

function drawCandleChart(canvasId, labels, opens, closes) {{
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.offsetWidth || 400;
  const h = 200;
  canvas.width = w;
  canvas.height = h;
  const n = labels.length;
  const pad = {{l:45, r:10, t:10, b:30}};
  const chartW = w - pad.l - pad.r;
  const chartH = h - pad.t - pad.b;
  const allPrices = [...opens, ...closes].filter(v => v !== null);
  const minP = Math.min(...allPrices) * 0.995;
  const maxP = Math.max(...allPrices) * 1.005;
  const scaleY = v => pad.t + chartH - ((v - minP) / (maxP - minP)) * chartH;
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = '#f1f3f5';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {{
    const y = pad.t + (chartH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    const val = maxP - ((maxP - minP) / 4) * i;
    ctx.fillStyle = '#868e96';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(val >= 1000 ? Math.round(val).toLocaleString() : val.toFixed(1), pad.l - 4, y + 3);
  }}
  const candleW = Math.max(2, Math.floor(chartW / n) - 2);
  for (let i = 0; i < n; i++) {{
    if (opens[i] === null || closes[i] === null) continue;
    const x = pad.l + (chartW / n) * i + (chartW / n) / 2;
    const o = scaleY(opens[i]);
    const c = scaleY(closes[i]);
    ctx.fillStyle = closes[i] >= opens[i] ? '#f03e3e' : '#1971c2';
    ctx.fillRect(x - candleW/2, Math.min(o,c), candleW, Math.max(1, Math.abs(o-c)));
  }}
  ctx.fillStyle = '#868e96';
  ctx.font = '10px sans-serif';
  ctx.textAlign = 'center';
  const step = Math.floor(n / 6);
  for (let i = 0; i < n; i += step) {{
    const x = pad.l + (chartW / n) * i + (chartW / n) / 2;
    ctx.fillText(labels[i], x, h - 5);
  }}
}}

sort('mcap');
</script>
</body>
</html>"""
    with open("screener.html", "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()