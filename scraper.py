import os
import json
import requests
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

# 출발 공항별 노선 (실제 운항 여부는 첫 실행 로그로 확인 후 정리할 것)
ROUTES = {
    "ICN": [
        ("NRT", "도쿄(나리타)"), ("HND", "도쿄(하네다)"), ("KIX", "오사카"), ("FUK", "후쿠오카"),
        ("CTS", "삿포로"), ("NGO", "나고야"), ("OKA", "오키나와"),
        ("KOJ", "가고시마"), ("KMJ", "구마모토"), ("OIT", "오이타"),
        ("TAK", "다카마쓰"), ("MYJ", "마쓰야마"), ("HIJ", "히로시마"),
        ("SDJ", "센다이"), ("FSZ", "시즈오카"), ("NGS", "나가사키"),
        ("KMI", "미야자키"), ("YGJ", "요나고"),
    ],
    "GMP": [("HND", "도쿄(하네다)"), ("KIX", "오사카")],
    "CJJ": [("NRT", "도쿄(나리타)"), ("KIX", "오사카"), ("FUK", "후쿠오카"), ("CTS", "삿포로")],
}

API_URL = "https://ignav.com/api/fares/round-trip"  # Ignav 문서 Round-Trip Fares 기준
API_KEY = os.environ.get("IGNAV_API_KEY", "")
KST = ZoneInfo("Asia/Seoul")
NEAR_DAYS = 21  # 이 기간 안의 구간은 매일, 그 밖은 일요일에만 검색


def load_schedule():
    with open("schedule.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_windows(schedule_data, today):
    days = []
    for ym in sorted(schedule_data.keys()):
        y, m = map(int, ym.split("-"))
        for i, c in enumerate(schedule_data[ym]):
            days.append((datetime(y, m, i + 1).date(), c))
    windows, i = [], 0
    while i < len(days):
        if days[i][1] in ("O", "V"):
            j = i
            while j + 1 < len(days) and days[j + 1][1] in ("O", "V"):
                j += 1
            if j > i and days[i][0] >= today:  # 이미 시작한 구간은 제외
                windows.append((days[i][0], days[j][0]))
            i = j + 1
        else:
            i += 1
    return windows


def first_seg(leg):
    segs = (leg or {}).get("segments") or []
    return segs[0] if segs else {}


def fetch_price(origin, dest, dep, ret):
    body = {"origin": origin, "destination": dest,
            "departure_date": dep, "return_date": ret, "market": "KR"}  # KR = 원화
    try:
        res = requests.post(API_URL, json=body, timeout=30,
                            headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"})
        if res.status_code != 200:
            print(f"[{res.status_code}] {origin}->{dest} {dep}: {res.text[:200]}")
            return None
        its = [x for x in res.json().get("itineraries", [])
               if isinstance(x.get("price"), dict) and x["price"].get("amount") is not None]
        if not its:
            print(f"[결과 없음] {origin}->{dest} {dep}")
            return None
        best = min(its, key=lambda x: x["price"]["amount"])
        out, inb = first_seg(best.get("outbound")), first_seg(best.get("inbound"))
        return {
            "price": best["price"]["amount"],
            "currency": best["price"].get("currency", ""),
            "airline": (best.get("outbound") or {}).get("carrier", "") or out.get("marketing_carrier_code", ""),
            "depart": (out.get("departure_time_local") or "")[:16],
            "arrive": (out.get("arrival_time_local") or "")[:16],
            "return_depart": (inb.get("departure_time_local") or "")[:16],
            "stops": max(len((best.get("outbound") or {}).get("segments") or []) - 1, 0),
            "ignav_id": best.get("ignav_id", ""),
        }
    except Exception as e:
        print(f"[에러] {origin}->{dest} {dep}: {e}")
        return None


def search_link(origin, dest, dep, ret):
    # 예약 링크 API는 별도 호출(추가 사용량)이라 무료 검색 링크로 대신함
    q = f"Flights from {origin} to {dest} on {dep} through {ret}"
    return "https://www.google.com/travel/flights?q=" + quote(q)


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("IGNAV_API_KEY가 없습니다. 저장소 Secrets에 등록하세요.")

    now = datetime.now(KST)
    today = now.date()
    windows = get_windows(load_schedule(), today)
    valid_starts = {d.isoformat() for d, _ in windows}

    # 기존 데이터 불러오되, 지나갔거나 근무표에서 사라진 구간은 버림
    existing = {}
    if os.path.exists("latest_prices.json"):
        try:
            with open("latest_prices.json", "r", encoding="utf-8") as f:
                for fare in json.load(f).get("fares", []):
                    if fare.get("window_start") in valid_starts:
                        existing[(fare["window_start"], fare["from"], fare["to"])] = fare
        except Exception as e:
            print(f"기존 파일 읽기 실패: {e}")

    is_sunday = now.weekday() == 6
    ok = tried = 0
    for start, end in windows:
        if (start - today).days > NEAR_DAYS and not is_sunday:
            continue
        dep, ret = start.isoformat(), end.isoformat()
        for origin, dests in ROUTES.items():
            for code, city in dests:
                tried += 1
                info = fetch_price(origin, code, dep, ret)
                if not info:
                    continue
                ok += 1
                existing[(dep, origin, code)] = {
                    "window_start": dep, "window_end": ret,
                    "from": origin, "to": code, "city": city, **info,
                    "link": search_link(origin, code, dep, ret),
                    "checked_at": now.isoformat(timespec="minutes"),
                }

    print(f"검색 {tried}건, 성공 {ok}건")
    if tried and ok == 0:
        raise SystemExit("성공 0건: 기존 데이터를 보존하고 종료합니다.")

    with open("latest_prices.json", "w", encoding="utf-8") as f:
        json.dump({"updated_at": now.isoformat(timespec="minutes"),
                   "fares": sorted(existing.values(), key=lambda x: (x["window_start"], x["price"]))},
                  f, ensure_ascii=False, indent=2)
    print(f"저장 완료: 총 {len(existing)}건")
