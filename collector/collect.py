"""쭈로그 수집기 — 내 네이버 블로그 현황.

네이버 공개 데이터(RSS, 글 목록, 방문자 그래프, 블로그 검색)를 모아
data/latest.json 과 data/history.json 을 만든다. 외부 패키지 없이 표준 라이브러리만 쓴다.

환경변수
  BLOG_ID                  config.json 의 blog_id 대신 쓸 블로그 아이디
  NAVER_CLIENT_ID/SECRET   있으면 네이버 검색 API 사용(정확·안정), 없으면 검색 페이지를 직접 읽음
"""

import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# 쭈로그 성장 단계: 5단계 × 3칸
GRADES = ["씨앗 1", "씨앗 2", "씨앗 3", "새싹 1", "새싹 2", "새싹 3",
          "꽃 1", "꽃 2", "꽃 3", "나무 1", "나무 2", "나무 3", "숲 1", "숲 2", "숲 3"]


def now_kst():
    return datetime.now(KST)


def http_get(url, headers=None, timeout=20):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.status, res.read().decode("utf-8", errors="replace")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 블로그 데이터

def fetch_rss(blog_id):
    """프로필과 최근 글(태그·요약 포함)."""
    _, body = http_get(f"https://rss.blog.naver.com/{blog_id}.xml")
    root = ET.fromstring(body)
    ch = root.find("channel")
    profile = {
        "title": (ch.findtext("title") or "").strip(),
        "description": (ch.findtext("description") or "").strip(),
        "image": (ch.findtext("image/url") or "").strip(),
        "link": f"https://blog.naver.com/{blog_id}",
    }
    posts = {}
    for it in ch.findall("item"):
        guid = it.findtext("guid") or it.findtext("link") or ""
        m = re.search(r"/(\d{9,})", guid)
        if not m:
            continue
        tags = [t.strip() for t in (it.findtext("tag") or "").split(",") if t.strip()]
        pub = it.findtext("pubDate") or ""
        try:
            date = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
        except ValueError:
            date = None
        posts[m.group(1)] = {
            "title": (it.findtext("title") or "").strip(),
            "category": (it.findtext("category") or "").strip(),
            "tags": tags,
            "date": date.isoformat() if date else None,
        }
    return profile, posts


def to_int(s):
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else 0


def parse_list_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", s)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=KST)
    m = re.match(r"(\d+)\s*(분|시간)\s*전", s)  # 오늘 쓴 글은 "3시간 전" 형태
    if m:
        delta = timedelta(minutes=int(m[1])) if m[2] == "분" else timedelta(hours=int(m[1]))
        return now_kst() - delta
    return now_kst()


def fetch_post_list(blog_id, weeks=12, max_pages=6):
    """글 목록 API: 총 글 수 + 최근 N주 글(날짜·댓글 수)."""
    since = now_kst() - timedelta(weeks=weeks)
    out, total = [], None
    for page in range(1, max_pages + 1):
        url = ("https://blog.naver.com/PostTitleListAsync.naver?"
               + urllib.parse.urlencode({"blogId": blog_id, "viewdate": "", "currentPage": page,
                                         "categoryNo": 0, "parentCategoryNo": "", "countPerPage": 30}))
        _, body = http_get(url, {"Referer": f"https://blog.naver.com/{blog_id}"})
        # 응답에 \' 같은 비표준 이스케이프가 섞여 있어 정리 후 파싱
        data = json.loads(body.replace("\\'", "'"))
        if data.get("resultCode") != "S":
            break
        total = to_int(data.get("totalCount"))
        rows = data.get("postList") or []
        for p in rows:
            d = parse_list_date(p.get("addDate"))
            out.append({
                "logNo": p["logNo"],
                "title": html.unescape(urllib.parse.unquote_plus(p.get("title", ""))),
                "date": d.isoformat(),
                "comments": to_int(p.get("commentCount")),
                "searchable": p.get("searchYn") == "true",
            })
        if not rows or parse_list_date(rows[-1].get("addDate")) < since:
            break
        time.sleep(0.5)
    return total, out


def fetch_visitors(blog_id):
    """최근 5일 방문자 수. 블로그 주인이 카운터를 숨기면 None."""
    try:
        status, body = http_get(f"https://blog.naver.com/NVisitorgp4Ajax.naver?blogId={blog_id}",
                                {"Referer": f"https://blog.naver.com/{blog_id}"})
    except urllib.error.URLError:
        return None
    if status != 200 or not body.strip():
        return None
    rows = re.findall(r'id="(\d{8})"\s+cnt="(\d+)"', body)
    if not rows:
        return None
    return [{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "count": int(c)} for d, c in rows]


# ---------------------------------------------------------------- 검색 노출

class Search:
    """블로그 검색 결과에서 '내 블로그 글'의 위치를 찾는다."""

    def __init__(self, blog_id):
        self.blog_id = blog_id.lower()
        self.cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
        self.secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
        self.mode = "api" if self.cid and self.secret else "web"
        self.calls = 0

    def _results(self, query, sort="sim"):
        """검색 결과 블로그 글 (blogId, logNo) 목록. 순서 = 순위."""
        self.calls += 1
        time.sleep(0.3 if self.mode == "api" else 1.2)
        if self.mode == "api":
            url = ("https://openapi.naver.com/v1/search/blog.json?"
                   + urllib.parse.urlencode({"query": query, "display": 100, "sort": sort}))
            _, body = http_get(url, {"X-Naver-Client-Id": self.cid,
                                     "X-Naver-Client-Secret": self.secret})
            links = [i.get("link", "") for i in json.loads(body).get("items", [])]
            text = "\n".join(links)
        else:
            params = {"ssc": "tab.blog.all", "query": query}
            if sort == "date":
                params["nso"] = "so:dd,p:all"
            _, text = http_get("https://search.naver.com/search.naver?" + urllib.parse.urlencode(params))
        seen, out = set(), []
        for bid, log in re.findall(r"blog\.naver\.com/([A-Za-z0-9_\-]+)/(\d{9,})", text):
            key = (bid.lower(), log)
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def exposure(self, title, log_no):
        """제목 그대로 검색해 내 글이 보이면 노출, 정확도·최신순 모두 없으면 누락."""
        try:
            for sort in ("sim", "date"):
                if (self.blog_id, log_no) in self._results(title, sort):
                    return "노출"
            return "누락"
        except (urllib.error.URLError, ValueError, TimeoutError):
            return "에러"

    def rank(self, keyword, log_no=None):
        """키워드 검색 시 내 글(log_no 지정 시 그 글, 아니면 내 블로그 아무 글)의 순위."""
        try:
            res = self._results(keyword)
        except (urllib.error.URLError, ValueError, TimeoutError):
            return {"rank": None, "error": True}
        for i, (bid, log) in enumerate(res, 1):
            if bid == self.blog_id and (log_no is None or log == log_no):
                return {"rank": i, "of": len(res)}
        return {"rank": None, "of": len(res)}


# ---------------------------------------------------------------- 지수 추정

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def rank_score(r):
    if not r:
        return 0.0
    if r <= 3:
        return 1.0
    if r <= 10:
        return 0.8
    if r <= 30:
        return 0.5
    if r <= 100:
        return 0.2
    return 0.0


def estimate_index(posts_12w, checked, visitors):
    """쭈로그 점수: 각 요소 0~1 점수 × 가중치, 값이 없는 요소는 빼고 정규화."""
    weekly = len(posts_12w) / 12
    weeks_active = len({datetime.fromisoformat(p["date"]).isocalendar()[:2] for p in posts_12w})
    judged = [c for c in checked if c["status"] in ("노출", "누락")]
    exposure = (sum(c["status"] == "노출" for c in judged) / len(judged)) if judged else None
    ranked = [c for c in checked if c.get("keyword")]
    kw = (sum(rank_score(c["keyword"].get("rank")) for c in ranked) / len(ranked)) if ranked else None
    comments = (sum(p["comments"] for p in posts_12w) / len(posts_12w)) if posts_12w else 0
    vis_avg = (sum(v["count"] for v in visitors) / len(visitors)) if visitors else None

    factors = [
        ("exposure", "검색 노출률", 40, exposure),
        ("keyword", "키워드 순위", 20, kw),
        ("pace", "포스팅 페이스", 15, clamp(weekly / 7)),
        ("consistency", "꾸준함(활동 주)", 10, clamp(weeks_active / 12)),
        ("engagement", "댓글 반응", 10, clamp(math.log1p(comments) / math.log1p(20))),
        ("visitors", "방문자", 5, None if vis_avg is None else clamp(math.log1p(vis_avg) / math.log1p(1000))),
    ]
    usable = [f for f in factors if f[3] is not None]
    wsum = sum(f[2] for f in usable) or 1
    score = round(sum(f[2] * f[3] for f in usable) / wsum * 100, 1)
    grade_i = min(len(GRADES) - 1, int(score / 100 * len(GRADES)))
    low_quality = bool(judged and len(judged) >= 5 and exposure is not None and exposure < 0.3)
    return {
        "score": score,
        "grade": GRADES[grade_i],
        "grade_index": grade_i,
        "grades": GRADES,
        "low_quality_risk": low_quality,
        "factors": [{"key": k, "label": lbl, "weight": w,
                     "value": None if v is None else round(v, 3)} for k, lbl, w, v in factors],
        "stats": {
            "weekly_posts": round(weekly, 1),
            "weeks_active": weeks_active,
            "exposure_rate": None if exposure is None else round(exposure, 3),
            "avg_comments": round(comments, 1),
            "visitors_avg": None if vis_avg is None else round(vis_avg, 1),
        },
    }


# ---------------------------------------------------------------- 실행

def main():
    cfg = load_json(os.path.join(ROOT, "config.json"), {})
    blog_id = (os.environ.get("BLOG_ID") or cfg.get("blog_id") or "").strip()
    stamp = now_kst()
    if not blog_id:
        save_json(os.path.join(DATA_DIR, "latest.json"),
                  {"configured": False, "updated_at": stamp.isoformat()})
        print("config.json 의 blog_id 가 비어 있습니다. 아이디를 넣고 다시 실행하세요.")
        return 0

    max_check = int(cfg.get("max_check_posts", 15))
    print(f"[{blog_id}] 수집 시작")
    profile, rss = fetch_rss(blog_id)
    total, plist = fetch_post_list(blog_id)
    visitors = fetch_visitors(blog_id)
    since = stamp - timedelta(weeks=12)
    posts_12w = [p for p in plist if datetime.fromisoformat(p["date"]) >= since]

    search = Search(blog_id)
    print(f"검색 모드: {search.mode}")
    checked = []
    for p in plist[:max_check]:
        meta = rss.get(p["logNo"], {})
        tags = meta.get("tags", [])
        item = {
            "logNo": p["logNo"],
            "title": p["title"],
            "url": f"https://blog.naver.com/{blog_id}/{p['logNo']}",
            "date": p["date"],
            "comments": p["comments"],
            "category": meta.get("category", ""),
            "tags": tags,
        }
        item["status"] = search.exposure(p["title"], p["logNo"]) if p["searchable"] else "검색 비허용"
        if tags:
            item["keyword"] = {"keyword": tags[0], **search.rank(tags[0], p["logNo"])}
        checked.append(item)
        print(f"  {item['status']:<4} {p['title'][:40]}")

    watch = []
    for kw in cfg.get("keywords", []):
        watch.append({"keyword": kw, **search.rank(kw)})
        print(f"  키워드 '{kw}': {watch[-1].get('rank')}")

    idx = estimate_index(posts_12w, checked, visitors)
    last_post = plist[0]["date"] if plist else None
    latest = {
        "configured": True,
        "blog_id": blog_id,
        "updated_at": stamp.isoformat(),
        "search_mode": search.mode,
        "search_calls": search.calls,
        "profile": profile,
        "total_posts": total,
        "last_post": last_post,
        "posts_12w": len(posts_12w),
        "visitors": visitors,
        "index": idx,
        "posts": checked,
        "watch_keywords": watch,
    }
    save_json(os.path.join(DATA_DIR, "latest.json"), latest)

    hist_path = os.path.join(DATA_DIR, "history.json")
    history = [h for h in load_json(hist_path, []) if h.get("blog_id") == blog_id]
    today = stamp.strftime("%Y-%m-%d")
    history = [h for h in history if h["date"] != today]
    history.append({
        "date": today,
        "blog_id": blog_id,
        "score": idx["score"],
        "grade": idx["grade"],
        "exposure_rate": idx["stats"]["exposure_rate"],
        "visitors_today": visitors[-1]["count"] if visitors else None,
        "total_posts": total,
        "keywords": {w["keyword"]: w.get("rank") for w in watch},
    })
    save_json(hist_path, history[-400:])
    print(f"완료: {idx['grade']} ({idx['score']}점), 노출률 {idx['stats']['exposure_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
