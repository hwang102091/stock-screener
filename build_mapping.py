"""
최초 1회 실행: python build_mapping.py
결과: mapping.json 생성 (ticker → corp_code, sector)
섹터: 네이버 금융 업종분류 사용 (KRX 인증 우회)
"""

import os, io, json, zipfile, time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
DART_KEY = os.getenv("DART_API_KEY")
OUT      = "mapping.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 네이버 금융 업종코드 → 섹터명
NAVER_SECTOR_MAP = {
    "1": "IT",           # 전기전자
    "2": "소재",         # 화학
    "3": "소재",         # 철강금속
    "4": "소재",         # 비금속광물
    "5": "소재",         # 종이목재
    "6": "에너지",       # 에너지
    "7": "산업재",       # 기계장비
    "8": "소비재",       # 운수장비
    "9": "산업재",       # 조선
    "10": "필수소비재",  # 음식료품
    "11": "소비재",      # 섬유의복
    "12": "헬스케어",    # 의약품
    "13": "헬스케어",    # 의료정밀
    "14": "유통업",      # 유통업
    "15": "산업재",      # 운수창고업
    "16": "IT서비스",    # 통신업
    "17": "금융",        # 금융업
    "18": "금융",        # 증권
    "19": "금융",        # 보험
    "20": "산업재",      # 건설업
    "21": "IT서비스",    # 서비스업
    "22": "유틸리티",    # 전기가스업
    "23": "기타",        # 기타
}

# ────────────────────────────────────────────
# 1. DART corp_code
# ────────────────────────────────────────────

def fetch_dart_corp_codes() -> dict:
    print("[1/3] DART corp_code ZIP 다운로드...")
    r = requests.get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": DART_KEY},
        timeout=30,
    )
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml_name = [n for n in zf.namelist() if n.endswith(".xml")][0]
    tree = ET.parse(zf.open(xml_name))
    result = {}
    for item in tree.getroot().findall("list"):
        code = item.findtext("stock_code", "").strip()
        if code:
            result[code] = {
                "corp_code": item.findtext("corp_code", "").strip(),
                "corp_name": item.findtext("corp_name", "").strip(),
            }
    print(f"  → 상장기업 {len(result)}개 매핑 완료")
    return result


# ────────────────────────────────────────────
# 2. 네이버 금융 업종별 종목 수집
# ────────────────────────────────────────────

def fetch_naver_sector_page(sector_code: str, page: int) -> list[dict]:
    """네이버 금융 업종별 종목 한 페이지 수집"""
    url = "https://finance.naver.com/sise/sise_group_detail.naver"
    headers = {
        "User-Agent": UA,
        "Referer": "https://finance.naver.com/sise/sise_group.naver?type=upjong",
    }
    r = requests.get(url, params={
        "type": "upjong",
        "no":   sector_code,
        "page": page,
    }, headers=headers, timeout=10)
    r.encoding = "EUC-KR"

    result = []
    # 종목코드 파싱: href='/item/main.naver?code=XXXXXX'
    import re
    codes = re.findall(r"code=(\d{6})", r.text)
    for code in set(codes):
        result.append(code)
    return result

def fetch_naver_sectors() -> dict:
    """네이버 금융 전체 업종 순회 → {ticker: sector} 반환"""
    print("[2/3] 네이버 금융 업종 데이터 수집...")
    ticker_to_sector = {}

    # 네이버 업종 목록 가져오기
    url = "https://finance.naver.com/sise/sise_group.naver"
    headers = {"User-Agent": UA}
    r = requests.get(url, params={"type": "upjong"}, headers=headers, timeout=10)
    r.encoding = "EUC-KR"

    import re
    # 업종코드와 업종명 파싱
    sector_entries = re.findall(
        r'no=(\d+)[^"]*"[^>]*>([^<]+)</a>',
        r.text
    )

    # 업종명 → 섹터명 직접 매핑
    NAME_TO_SECTOR = {
        "전기전자": "IT", "화학": "소재", "철강금속": "소재",
        "비금속광물": "소재", "종이목재": "소재", "에너지": "에너지",
        "기계장비": "산업재", "운수장비": "소비재", "조선": "산업재",
        "음식료품": "필수소비재", "섬유의복": "소비재", "의약품": "헬스케어",
        "의료정밀": "헬스케어", "유통업": "소비재", "운수창고업": "산업재",
        "통신업": "IT서비스", "금융업": "금융", "증권": "금융",
        "보험": "금융", "건설업": "산업재", "서비스업": "IT서비스",
        "전기가스업": "유틸리티", "반도체": "IT", "디스플레이": "IT",
        "소프트웨어": "IT서비스", "인터넷": "IT서비스", "게임": "IT서비스",
        "바이오": "헬스케어", "제약": "헬스케어", "2차전지": "배터리",
        "자동차": "소비재",
    }

    total_sectors = len(sector_entries)
    print(f"  업종 {total_sectors}개 발견")

    for idx, (no, name) in enumerate(sector_entries):
        name = name.strip()
        sector = "기타"
        for key, val in NAME_TO_SECTOR.items():
            if key in name:
                sector = val
                break

        # 해당 업종 종목 수집 (최대 5페이지)
        tickers_in_sector = []
        for page in range(1, 6):
            try:
                tickers = fetch_naver_sector_page(no, page)
                if not tickers:
                    break
                tickers_in_sector.extend(tickers)
                time.sleep(0.1)
            except Exception:
                break

        for ticker in set(tickers_in_sector):
            ticker_to_sector[ticker] = sector

        if (idx + 1) % 5 == 0:
            print(f"  {idx+1}/{total_sectors} 업종 처리 중... ({len(ticker_to_sector)}개 누적)")
        time.sleep(0.2)

    # 샘플 확인
    for t in ["005930", "000660", "005380", "035420"]:
        if t in ticker_to_sector:
            print(f"  샘플 {t}: {ticker_to_sector[t]}")

    print(f"  → 최종 {len(ticker_to_sector)}개 종목 섹터 매핑 완료")
    return ticker_to_sector


# ────────────────────────────────────────────
# 3. DART ROE 샘플 검증
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

def calc_roe(corp_code, year):
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

def verify_roe_sample(ticker_to_corp):
    print("[3/3] ROE 조회 검증 (5개 샘플)...")
    year = str(datetime.today().year - 1)
    for ticker in ["005930", "000660", "005380", "035420", "005490"]:
        info = ticker_to_corp.get(ticker, {})
        corp_code = info.get("corp_code")
        if not corp_code:
            print(f"  {ticker}: corp_code 없음")
            continue
        roe  = calc_roe(corp_code, year)
        name = info.get("corp_name", ticker)
        print(f"  {ticker} ({name}): ROE = {roe}%" if roe is not None
              else f"  {ticker} ({name}): ROE 조회 실패")
        time.sleep(0.3)


# ────────────────────────────────────────────
# 4. mapping.json 저장
# ────────────────────────────────────────────

def main():
    ticker_to_corp   = fetch_dart_corp_codes()
    ticker_to_sector = fetch_naver_sectors()
    verify_roe_sample(ticker_to_corp)

    merged = {}
    for t in set(ticker_to_corp) | set(ticker_to_sector):
        info = ticker_to_corp.get(t, {})
        merged[t] = {
            "corp_code": info.get("corp_code", ""),
            "corp_name": info.get("corp_name", ""),
            "sector":    ticker_to_sector.get(t, "기타"),
        }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "built_at": datetime.now().isoformat(),
            "count":    len(merged),
            "mapping":  merged,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n=== mapping.json 저장 완료: {len(merged)}개 종목 ===")
    print("다음 단계: python data_collector.py")

if __name__ == "__main__":
    main()