import os
import json
import requests
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

# ===== 사용량 절약 설정 (여기 숫자만 바꾸면 됨) =====
MONTHLY_BUDGET = 300   # 한 달 최대 API 호출 수. 넘으면 그달은 더 검색 안 함
NEAR_DAYS = 21         # 출발까지 21일 이내 구간은
NEAR_EVERY = 3         #   3일에 한 번 검색
FAR_DAYS = 60          # 21~60일 구간은 일요일에만, 60일 넘는 구간은 검색 안 함
DEAD_SKIP_DAYS = 30    # 결과가 하나도 없던 노선은 30일 동안 건너뜀
WORKERS = 6            # 동시에 검색하는 개수 (속도)
# ===================================================

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

API_URL = "https://ignav.com/api/fares/round-trip"
API_KEY = os.environ.get("IGNAV_API_KEY", "")
KST = ZoneInfo("Asia/Seoul")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_windows(schedule, today):
    days = []
    for ym in sorted(schedule):
        y, m = map(int, ym.split("-"))
        for i, c in enumerate(schedule[ym]):
            days.append((datetime(y, m, i + 1).date(), c))
    out, i = [], 0
    while i < len(days):
        if days[i][1] in ("O", "V"):
            j = i
            while j + 1 < len(days) and days[j + 1][1] in ("O", "V"):
                j += 1
            if j > i and days[i][0] >= today:
                out.append((days[i][0], days[j][0]))
            i = j + 1
        else:
            i += 1
    return out


def first_seg(leg):
    segs = (leg or {}).get("segments") or []
    return segs[0] if segs else {}


def fetch(task):
    origin, dest, city, dep, ret = task
    body = {"origin": origin, "destination": dest,
            "departure_date": dep, "return_date": ret, "market": "KR"}
    try:
        r = requests.post(API_URL, json=body, timeout=30,
                          headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"})
        if r.status_code != 200:
            print(f"[에러 {r.status_code}] {origin}->{dest} {dep}: {r.text[:150]}")
            return task, "error", None
        its = [x for x in r.json().get("itineraries", [])
               if isinstance(x.get("price"), dict) and x["price"].get("amount") is not None]
        if not its:
            print(f"[결과 없음] {origin}->{dest} {dep}")
            return task, "empty", None
        b = min(its, key=lambda x: x["price"]["amount"])
        out, inb = first_seg(b.get("outbound")), first_seg(b.get("inbound"))
        print(f"[OK] {origin}->{dest} {dep} {b['price']['amount']}")
        return task, "ok", {
            "price": b["price"]["amount"],
            "currency": b["price"].get("currency", ""),
            "airline": (b.get("outbound") or {}).get("carrier", "") or out.get("marketing_carrier_code", ""),
            "depart": (out.get("departure_time_local") or "")[:16],
            "arrive": (out.get("arrival_time_local") or "")[:16],
            "return_depart": (inb.get("departure_time_local") or "")[:16],
            "stops": max(len((b.get("outbound") or {}).get("segments") or []) - 1, 0),
        }
    except Exception as e:
        print(f"[에러] {origin}->{dest} {dep}: {e}")
        return task, "error", None


def search_link(o, d, dep, ret):
    return "https://www.google.com/travel/flights?q=" + quote(f"Flights from {o} to {d} on {dep} through {ret}")


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("IGNAV_API_KEY가 없습니다. 저장소 Secrets에 등록하세요.")

    now = datetime.now(KST)
    today = now.date()
    month = today.strftime("%Y-%m")
    state = load_json("state.json", {"usage": {}, "dead": {}})
    used = state["usage"].get(month, 0)
    left = MONTHLY_BUDGET - used

    windows = get_windows(load_json("schedule.json", {}), today)
    valid = {s.isoformat() for s, _ in windows}

    # 1) 이번에 검색할 목록 정하기
    tasks = []
    for start, end in windows:
        d = (start - today).days
        near_day = d <= NEAR_DAYS and today.toordinal() % NEAR_EVERY == 0
        far_day = NEAR_DAYS < d <= FAR_DAYS and today.weekday() == 6
        if not (near_day or far_day):
            continue
        for o, dests in ROUTES.items():
            for code, city in dests:
                dead = state["dead"].get(f"{o}-{code}")
                if dead and (today - datetime.fromisoformat(dead).date()).days < DEAD_SKIP_DAYS:
                    continue
                tasks.append((o, code, city, start.isoformat(), end.isoformat()))
    tasks.sort(key=lambda t: t[3])  # 가까운 구간부터
    if len(tasks) > left:
        print(f"이번 달 남은 한도 {max(left, 0)}건 → {len(tasks)}건 중 일부만 검색")
        tasks = tasks[:max(left, 0)]
    print(f"이번 달 사용 {used}/{MONTHLY_BUDGET}, 이번 실행 {len(tasks)}건")

    # 2) 기존 데이터 (지나간 구간은 버림)
    old = load_json("latest_prices.json", {})
    fares = {(f["window_start"], f["from"], f["to"]): f
             for f in old.get("fares", []) if f.get("window_start") in valid}

    # 3) 검색 (동시에 여러 건)
    ok = err = 0
    route_hits = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for (o, code, city, dep, ret), status, info in ex.map(fetch, tasks):
            key = f"{o}-{code}"
            if status == "ok":
                ok += 1
                route_hits[key] = True
                fares[(dep, o, code)] = {
                    "window_start": dep, "window_end": ret, "from": o, "to": code, "city": city,
                    **info, "link": search_link(o, code, dep, ret),
                    "checked_at": now.isoformat(timespec="minutes"),
                }
            elif status == "empty":
                route_hits.setdefault(key, False)
            else:
                err += 1

    # 결과가 한 번도 없던 노선은 한동안 건너뛰기
    for key, hit in route_hits.items():
        if hit:
            state["dead"].pop(key, None)
        else:
            state["dead"][key] = today.isoformat()
            print(f"[노선 쉬기] {key}: {DEAD_SKIP_DAYS}일 동안 건너뜀")

    state["usage"][month] = used + len(tasks)
    save_json("state.json", state)
    print(f"검색 {len(tasks)}건 / 성공 {ok} / 에러 {err} / 이번 달 누적 {state['usage'][month]}")

    if tasks and ok == 0 and err > 0:
        raise SystemExit("성공 0건: 기존 가격 데이터를 보존하고 종료합니다.")

    save_json("latest_prices.json", {
        "updated_at": now.isoformat(timespec="minutes") if ok else old.get("updated_at", ""),
        "fares": sorted(fares.values(), key=lambda x: (x["window_start"], x["price"])),
    })
