"""쭈로그 수집기 — 내 네이버 블로그 현황.

네이버 공개 데이터(RSS, 글 목록, 모바일 블로그 정보·글 목록(공감·댓글), 글 본문, 방문자 그래프, 검색)를 모아
data/latest.json, data/history.json, data/visitors.json 을 만든다. 표준 라이브러리만 쓴다.

환경변수 (모두 선택)
  BLOG_ID                       config.json 의 blog_id 대신 쓸 블로그 아이디
  NAVER_CLIENT_ID / SECRET      NAVER API HUB 검색 API — 블로그탭 순위 100위까지, 문서 수
  NAVER_AD_CUSTOMER_ID / API_KEY / SECRET
                                네이버 검색광고 API — 키워드 월간 검색량·경쟁도
"""

import base64
import hashlib
import hmac
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

GRADES = ["일반", "준최1", "준최2", "준최3", "준최4", "준최5", "준최6", "준최7",
          "최적1", "최적2", "최적3", "최적1+", "최적2+", "최적3+", "최적4+"]

NET_ERRORS = (urllib.error.URLError, ValueError, TimeoutError, ConnectionError)


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


def to_int(s):
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else 0


# ---------------------------------------------------------------- 블로그 데이터

def fetch_rss(blog_id):
    """프로필과 최근 글(태그·카테고리)."""
    _, body = http_get(f"https://rss.blog.naver.com/{blog_id}.xml")
    ch = ET.fromstring(body).find("channel")
    profile = {
        "title": (ch.findtext("title") or "").strip(),
        "description": (ch.findtext("description") or "").strip(),
        "image": (ch.findtext("image/url") or "").strip(),
        "link": f"https://blog.naver.com/{blog_id}",
    }
    posts = {}
    for it in ch.findall("item"):
        m = re.search(r"/(\d{9,})", it.findtext("guid") or it.findtext("link") or "")
        if m:
            posts[m.group(1)] = {
                "category": (it.findtext("category") or "").strip(),
                "tags": [t.strip() for t in (it.findtext("tag") or "").split(",") if t.strip()],
            }
    return profile, posts


def fetch_blog_info(blog_id):
    """모바일 블로그 정보: 구독자·누적 방문·주제·네이버 메이트."""
    try:
        _, body = http_get(f"https://m.blog.naver.com/api/blogs/{blog_id}",
                           {"Referer": f"https://m.blog.naver.com/{blog_id}"})
        r = json.loads(body).get("result") or {}
    except NET_ERRORS:
        return {}
    return {
        "name": r.get("blogName"),
        "nickname": r.get("nickName"),
        "subscribers": r.get("subscriberCount"),
        "total_visitors": r.get("totalVisitorCount"),
        "today_visitors": r.get("dayVisitorCount"),
        "topic": r.get("blogDirectoryName"),
        "power_blog": bool(r.get("powerBlog")),
        "naver_mate": bool(r.get("isNaverMateBlog")),
        "mate_citations": r.get("mateCitations"),
        "official": bool(r.get("officialBlog")),
    }


def parse_list_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", s)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), 12, tzinfo=KST)
    m = re.match(r"(\d+)\s*(분|시간)\s*전", s)  # 하루 안에 쓴 글은 "3시간 전" 형태
    if m:
        delta = timedelta(minutes=int(m[1])) if m[2] == "분" else timedelta(hours=int(m[1]))
        return now_kst() - delta
    return now_kst()


def fetch_post_list(blog_id, weeks=12, max_pages=6):
    """글 목록: 총 글 수 + 최근 N주 글(날짜·댓글 수·검색 허용)."""
    since = now_kst() - timedelta(weeks=weeks)
    out, total = [], None
    for page in range(1, max_pages + 1):
        url = ("https://blog.naver.com/PostTitleListAsync.naver?"
               + urllib.parse.urlencode({"blogId": blog_id, "viewdate": "", "currentPage": page,
                                         "categoryNo": 0, "parentCategoryNo": "", "countPerPage": 30}))
        _, body = http_get(url, {"Referer": f"https://blog.naver.com/{blog_id}"})
        data = json.loads(body.replace("\\'", "'"))  # 비표준 이스케이프 정리
        if data.get("resultCode") != "S":
            break
        total = to_int(data.get("totalCount"))
        rows = data.get("postList") or []
        for p in rows:
            out.append({
                "logNo": p["logNo"],
                "title": html.unescape(urllib.parse.unquote_plus(p.get("title", ""))),
                "date": parse_list_date(p.get("addDate")).isoformat(),
                "comments": to_int(p.get("commentCount")),
                "searchable": p.get("searchYn") == "true",
            })
        if not rows or parse_list_date(rows[-1].get("addDate")) < since:
            break
        time.sleep(0.5)
    return total, out


def fetch_post_stats(blog_id, max_pages=6):
    """모바일 글 목록: 글마다 공감·댓글·조회수(공개한 경우만)·썸네일. 인기 글 순위용."""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        try:
            _, body = http_get(f"https://m.blog.naver.com/api/blogs/{blog_id}/post-list?"
                               + urllib.parse.urlencode({"categoryNo": 0, "itemCount": 30, "page": page}),
                               {"Referer": f"https://m.blog.naver.com/{blog_id}"})
            items = (json.loads(body).get("result") or {}).get("items") or []
        except NET_ERRORS:
            break
        new = [it for it in items if str(it.get("logNo")) not in seen]
        for it in new:
            log_no = str(it["logNo"])
            seen.add(log_no)
            if it.get("notOpen"):
                continue
            thumbs = it.get("thumbnailList") or []
            read = it.get("readCount")
            out.append({
                "logNo": log_no,
                "title": html.unescape(it.get("titleWithInspectMessage") or ""),
                "url": f"https://blog.naver.com/{blog_id}/{log_no}",
                "date": datetime.fromtimestamp(it["addDate"] / 1000, KST).isoformat() if it.get("addDate") else None,
                "category": it.get("categoryName") or "",
                "likes": to_int(it.get("sympathyCnt")),
                "comments": to_int(it.get("commentCnt")),
                "views": to_int(read) if read not in (None, "") else None,  # 조회수 비공개면 None
                "thumb": thumbs[0].get("encodedThumbnailUrl") if thumbs else None,
            })
        if len(items) < 30 or not new:
            break
        time.sleep(0.4)
    return out


def fetch_visitors(blog_id):
    """최근 5일 방문자 수. 카운터가 비공개면 None."""
    try:
        status, body = http_get(f"https://blog.naver.com/NVisitorgp4Ajax.naver?blogId={blog_id}",
                                {"Referer": f"https://blog.naver.com/{blog_id}"})
    except NET_ERRORS:
        return None
    rows = re.findall(r'id="(\d{8})"\s+cnt="(\d+)"', body) if status == 200 else []
    return [{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "count": int(c)} for d, c in rows] or None


# ---------------------------------------------------------------- 글 본문 분석

def analyze_post(blog_id, log_no, title, keyword):
    """글 본문을 읽어 원고 지표와 최적화 점수(0~100)를 낸다."""
    try:
        _, body = http_get(f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}",
                           {"Referer": f"https://blog.naver.com/{blog_id}"})
    except NET_ERRORS:
        return None
    start = body.find("se-main-container")
    main = body[start:] if start >= 0 else body
    end = main.find('class="comment_area')
    main = main[:end] if end > 0 else main
    paragraphs = re.findall(r'<p class="se-text-paragraph[^"]*"[^>]*>(.*?)</p>', main, re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "", " ".join(paragraphs)))
    text = re.sub(r"[​\s]+", " ", text).strip()
    chars = len(text.replace(" ", ""))
    m = {
        "chars": chars,
        "images": main.count("se-image-resource"),
        "videos": main.count("se-video"),
        "quotes": main.count("se-quotation-container"),
        "headings": main.count("se-section-sectionTitle"),
        "links": main.count("se-oglink") // 2,
        "maps": min(main.count("se-placesMap"), 1),
        "title_len": len(title),
        # 띄어쓰기를 무시하고 센다 (태그는 붙여 쓰고 본문은 띄어 쓰는 경우가 많음)
        "kw_count": text.replace(" ", "").count(keyword.replace(" ", "")) if keyword else None,
    }

    checks = []  # (점수, 만점, 통과 여부, 팁)

    def add(ok_ratio, weight, tip):
        checks.append((weight * ok_ratio, weight, ok_ratio >= 0.99, tip))

    add(min(1, chars / 1500), 30, f"본문 {chars:,}자 — 1,500자 이상 권장")
    add(min(1, m["images"] / 8), 20, f"사진 {m['images']}장 — 8장 이상 권장")
    add(1 if 15 <= m["title_len"] <= 40 else 0.5, 10, f"제목 {m['title_len']}자 — 15~40자 권장")
    if keyword:
        k = m["kw_count"] or 0
        add(1 if 3 <= k <= 10 else (0.6 if k else 0), 20, f"대표 키워드 '{keyword}' 본문 {k}회 — 3~10회 권장")
    else:
        add(0, 20, "태그가 없어요 — 대표 키워드를 태그 첫 번째에 넣어보세요")
    add(1 if (m["headings"] + m["quotes"]) >= 2 else 0.5, 10, "소제목·인용구로 문단 나누기")
    add(1 if (m["videos"] or m["maps"]) else 0.5, 10, "동영상이나 지도 1개 이상")
    score = round(sum(c[0] for c in checks))
    m["score"] = score
    m["tips"] = [c[3] for c in checks if not c[2]]
    return m


# ---------------------------------------------------------------- 검색

def link_keys(text):
    """HTML/JSON 안의 블로그 글 링크 (blogId, logNo) 를 등장 순서대로."""
    seen, out = set(), []
    for bid, log in re.findall(r"blog\.naver\.com(?:\\?/|%2F)([A-Za-z0-9_\-]+)(?:\\?/|%2F)(\d{9,})", text):
        key = (bid.lower(), log)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


class Search:
    """검색 결과에서 '내 블로그 글'의 위치를 찾는다."""

    def __init__(self, blog_id):
        self.blog_id = blog_id.lower()
        self.cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
        self.secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
        self.mode = "api" if self.cid and self.secret else "web"
        self.calls = 0
        self.doc_counts = {}
        self.seen = {}  # 다른 블로그 id → {키워드: 최고 순위} (키워드 순위 검색에서 본 것)

    def _web(self, params):
        """네이버 검색 페이지. 너무 자주 부르면 403/429로 막히니 한 번 쉬었다 다시 시도."""
        url = "https://search.naver.com/search.naver?" + urllib.parse.urlencode(params)
        for attempt in range(2):
            self.calls += 1
            time.sleep(1.5 if not attempt else 45)
            try:
                _, text = http_get(url, {"Referer": "https://www.naver.com"})
                return text
            except urllib.error.HTTPError as e:
                if e.code not in (403, 429) or attempt:
                    raise
                print(f"  검색 차단({e.code}) — 45초 쉬고 다시 시도")

    def blog_tab(self, query, sort="sim"):
        """블로그탭 결과 순서 = 순위."""
        if self.mode == "api":
            self.calls += 1
            time.sleep(0.3)
            # 2026-07부터 검색 API는 네이버클라우드 NAVER API HUB 에서 발급·호출
            url = ("https://naverapihub.apigw.ntruss.com/search/v1/blog?"
                   + urllib.parse.urlencode({"query": query, "display": 100, "sort": sort, "format": "json"}))
            _, body = http_get(url, {"X-NCP-APIGW-API-KEY-ID": self.cid, "X-NCP-APIGW-API-KEY": self.secret})
            data = json.loads(body)
            self.doc_counts[query] = data.get("total")
            return link_keys("\n".join(i.get("link", "") for i in data.get("items", [])))
        params = {"ssc": "tab.blog.all", "query": query}
        if sort == "date":
            params["nso"] = "so:dd,p:all"
        return link_keys(self._web(params))

    def exposure(self, title, log_no):
        """제목 그대로 검색: 블로그탭(정확도·최신) 노출 여부 + 통합검색 노출 여부."""
        me = (self.blog_id, log_no)
        try:
            blog = "누락"
            for sort in ("sim", "date"):
                if me in self.blog_tab(title, sort):
                    blog = "노출"
                    break
        except NET_ERRORS:
            blog = "에러"
        try:
            total = "노출" if me in link_keys(self._web({"query": title})) else "미노출"
        except NET_ERRORS:
            total = "에러"
        return blog, total

    def rank(self, keyword, log_no=None):
        """키워드 블로그탭 순위 (log_no 지정 시 그 글, 아니면 내 블로그 아무 글)."""
        try:
            res = self.blog_tab(keyword)
        except NET_ERRORS:
            return {"rank": None, "error": True}
        for i, (bid, _) in enumerate(res[:30], 1):
            if bid != self.blog_id:
                kws = self.seen.setdefault(bid, {})
                kws[keyword] = min(kws.get(keyword, i), i)
        for i, (bid, log) in enumerate(res, 1):
            if bid == self.blog_id and (log_no is None or log == log_no):
                return {"rank": i, "of": len(res)}
        return {"rank": None, "of": len(res)}


class KeywordTool:
    """네이버 검색광고 API 키워드 도구 — 월간 검색량·경쟁도. 키가 없으면 비활성."""

    def __init__(self):
        self.cust = os.environ.get("NAVER_AD_CUSTOMER_ID", "").strip()
        self.key = os.environ.get("NAVER_AD_API_KEY", "").strip()
        self.secret = os.environ.get("NAVER_AD_SECRET", "").strip()
        self.enabled = bool(self.cust and self.key and self.secret)

    def volumes(self, keywords, cache, max_age_days=7):
        """월간 검색량. cache(키워드→값+날짜)를 먼저 쓰고 오래된 것만 새로 묻는다.
        API가 자주 불리면 큰 키워드도 "< 10" 으로 가려서 주는 경우가 있어,
        그런 응답이 알려진 큰 값을 덮어쓰지 않게 한다."""
        today = now_kst().strftime("%Y-%m-%d")
        fresh_after = (now_kst() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        wanted = list(dict.fromkeys(k.replace(" ", "") for k in keywords if k.strip()))
        todo = [k for k in wanted if not (k in cache and cache[k].get("date", "") >= fresh_after)]
        if self.enabled and todo:
            for kw, v in self._query(todo).items():
                old = cache.get(kw)
                masked = v["pc"] <= 5 and v["mobile"] <= 5
                if masked and old and old["total"] > 20:
                    old["date"] = today  # 가려진 응답 — 알던 값 유지
                    continue
                cache[kw] = {**v, "date": today}
        return {k: cache[k] for k in wanted if k in cache}

    def _query(self, keywords):
        out = {}
        # 여러 개를 한 번에 물으면 일부가 "< 10" 으로 잘못 오는 경우가 있어 하나씩 묻는다
        for kw in keywords:
            ts = str(int(time.time() * 1000))
            sig = base64.b64encode(hmac.new(self.secret.encode(), f"{ts}.GET./keywordstool".encode(),
                                            hashlib.sha256).digest()).decode()
            url = "https://api.searchad.naver.com/keywordstool?" + urllib.parse.urlencode(
                {"hintKeywords": kw, "showDetail": 1})
            try:
                _, body = http_get(url, {"X-Timestamp": ts, "X-API-KEY": self.key,
                                         "X-Customer": self.cust, "X-Signature": sig})
            except NET_ERRORS as e:
                print(f"  검색광고 API 실패 ({kw}): {e}")
                continue
            rows = json.loads(body).get("keywordList", [])
            row = next((r for r in rows if r.get("relKeyword", "").upper() == kw.upper()), None)
            if row:
                pc, mo = self._count(row.get("monthlyPcQcCnt")), self._count(row.get("monthlyMobileQcCnt"))
                out[kw] = {"pc": pc, "mobile": mo, "total": pc + mo, "competition": row.get("compIdx")}
            time.sleep(0.25)
        return out

    @staticmethod
    def _count(v):
        """검색광고 API는 10 미만을 "< 10" 문자열로 준다 → 5로 본다."""
        return 5 if isinstance(v, str) and "<" in v else to_int(v)


# ---------------------------------------------------------------- 지수·진단

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def rank_score(r):
    if not r:
        return 0.0
    for limit, s in ((3, 1.0), (10, 0.8), (30, 0.5), (100, 0.2)):
        if r <= limit:
            return s
    return 0.0


def keyword_power(k):
    """키워드 하나의 경쟁력 0~1: 순위 점수 × 검색량 크기(월 10만 = 1).
    검색량을 모르면 중간값(0.5)으로 본다."""
    vol = (k.get("volume") or {}).get("total")
    size = clamp(math.log10(vol + 1) / 5) if vol is not None else 0.5
    return rank_score(k.get("rank")) * size


# 등급 경계: 위로 갈수록 촘촘히 어려워지게 score = 100 × (i/15)^(1/2.2)
# → 준최1 29점, 준최4 55점, 최적1 75점, 최적4+ 98점
GRADE_CURVE = 2.2
GRADE_AT = [round(100 * (i / len(GRADES)) ** (1 / GRADE_CURVE), 1) for i in range(len(GRADES))]


def estimate_index(posts_12w, checked, visitors, info):
    """쭈로그 지수: 각 요소 0~1 × 가중치, 값이 없는 요소는 빼고 정규화.
    방문자 규모와 '얼마나 큰 키워드에서 상위에 뜨는지'가 등급을 좌우한다."""
    weekly = len(posts_12w) / 12
    weeks_active = len({datetime.fromisoformat(p["date"]).isocalendar()[:2] for p in posts_12w})
    judged = [c for c in checked if c["status"] in ("노출", "누락")]
    exposure = (sum(c["status"] == "노출" for c in judged) / len(judged)) if judged else None
    tj = [c for c in checked if c.get("total_search") in ("노출", "미노출")]
    total_exp = (sum(c["total_search"] == "노출" for c in tj) / len(tj)) if tj else None
    all_kws = [k for c in checked for k in c.get("keywords", [])]
    kws = [k for k in all_kws if not k.get("error")]
    ranks = [k.get("rank") for k in kws]
    # 태그 절반은 원래 큰 키워드라 평균의 2배를 만점으로. 검색이 절반 넘게 실패하면 판단 보류
    kw = clamp(2 * sum(keyword_power(k) for k in kws) / len(kws)) if kws and len(kws) * 2 >= len(all_kws) else None
    opt = [c["content"]["score"] for c in checked if c.get("content")]
    content = (sum(opt) / len(opt) / 100) if opt else None
    comments = (sum(p["comments"] for p in posts_12w) / len(posts_12w)) if posts_12w else 0
    subs = info.get("subscribers")
    engagement = clamp(0.5 * math.log1p(comments) / math.log1p(30)
                       + 0.5 * (math.log10(subs + 1) / 4 if subs else 0))
    # 오늘은 집계 중이라 빼고 평균
    full_days = visitors[:-1] if visitors and len(visitors) > 1 else visitors
    vis_avg = (sum(v["count"] for v in full_days) / len(full_days)) if full_days else None

    factors = [
        ("visitors", "방문자 규모", 30, None if vis_avg is None else clamp(math.log10(vis_avg + 1) / 4)),
        ("keyword", "키워드 경쟁력", 30, kw),
        ("exposure", "블로그탭 노출", 10, exposure),
        ("total", "통합검색 노출", 10, total_exp),
        ("pace", "발행 페이스", 10, clamp(weekly / 7)),
        ("consistency", "꾸준함", 5, clamp(weeks_active / 12)),
        ("engagement", "소통·구독", 5, engagement),
    ]
    usable = [f for f in factors if f[3] is not None]
    wsum = sum(f[2] for f in usable) or 1
    score = round(sum(f[2] * f[3] for f in usable) / wsum * 100, 1)
    gi = max(i for i, at in enumerate(GRADE_AT) if score >= at)
    return {
        "score": score,
        "grade": GRADES[gi],
        "grade_index": gi,
        "grades": GRADES,
        "grade_at": GRADE_AT,
        "factors": [{"key": k, "label": lbl, "weight": w, "value": None if v is None else round(v, 3)}
                    for k, lbl, w, v in factors],
        "stats": {
            "weekly_posts": round(weekly, 1),
            "weeks_active": weeks_active,
            "exposure_rate": None if exposure is None else round(exposure, 3),
            "total_exposure_rate": None if total_exp is None else round(total_exp, 3),
            "avg_comments": round(comments, 1),
            "avg_content_score": round(sum(opt) / len(opt)) if opt else None,
            "visitors_avg": None if vis_avg is None else round(vis_avg, 1),
            "top10_keywords": sum(1 for r in ranks if r and r <= 10),
            "best_top10_volume": max([(k.get("volume") or {}).get("total") or 0
                                      for k in kws if k.get("rank") and k["rank"] <= 10] or [0]),
        },
    }


def diagnose(idx, checked, plist, visitor_series):
    """블로그 진단 체크리스트. level: good / warn / bad."""
    st, out = idx["stats"], []

    def add(level, title, detail):
        out.append({"level": level, "title": title, "detail": detail})

    er = st["exposure_rate"]
    if er is not None:
        add("good" if er >= 0.8 else "warn" if er >= 0.5 else "bad", "블로그탭 노출",
            f"최근 글 {round(er * 100)}%가 블로그 검색에 보여요."
            + ("" if er >= 0.8 else " 누락된 글은 제목·본문 중복이나 금칙어를 점검해 보세요."))
    te = st["total_exposure_rate"]
    if te is not None:
        add("good" if te >= 0.5 else "warn" if te >= 0.2 else "bad", "통합검색 진입",
            f"최근 글 {round(te * 100)}%가 통합검색(통검)에도 떠요.")
    recent7 = sum(1 for p in plist if datetime.fromisoformat(p["date"]) >= now_kst() - timedelta(days=7))
    add("good" if recent7 >= 3 else "warn" if recent7 else "bad", "최근 7일 발행",
        f"최근 7일 동안 {recent7}개 발행. 주 3개 이상이 지수에 유리해요.")
    if len(plist) >= 2:
        dates = sorted(datetime.fromisoformat(p["date"]) for p in plist)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        mx = max(gaps)
        add("good" if mx <= 7 else "warn" if mx <= 14 else "bad", "발행 공백",
            f"최근 12주 중 가장 긴 공백 {mx}일.")
    cs = st["avg_content_score"]
    if cs is not None:
        add("good" if cs >= 80 else "warn" if cs >= 60 else "bad", "글 최적화",
            f"최근 글 평균 {cs}점. 글자 수·사진·키워드 반복을 맞추면 올라가요.")
    if len(visitor_series) >= 10:
        recent = sum(v["count"] for v in visitor_series[-5:]) / 5
        before = sum(v["count"] for v in visitor_series[-10:-5]) / 5
        if before:
            ch = (recent - before) / before
            add("good" if ch >= -0.1 else "warn" if ch >= -0.4 else "bad", "방문자 추세",
                f"최근 5일 평균 {recent:.0f}명, 그 전 5일 대비 {ch * 100:+.0f}%.")
    risk = bool(er is not None and len([c for c in checked if c['status'] in ('노출', '누락')]) >= 5 and er < 0.3)
    if risk:
        add("bad", "저품질 의심", "최근 글 대부분이 검색에 안 보여요. 블로그 전체가 밀려났을 수 있어요.")
    return out, risk


# ---------------------------------------------------------------- 다음 등급 로드맵

def level_plan(idx, checked, posts_12w, vis_avg):
    """다음 등급까지 몇 점이 필요한지, 무엇을 하면 몇 주 걸리는지 시뮬레이션.
    방문자·노출률·소통은 지금 그대로 둔다(보수적) — 발행과 키워드만 바꿔 본다."""
    gi = idx["grade_index"]
    if gi + 1 >= len(GRADES):
        return None
    weights = {f["key"]: f["weight"] for f in idx["factors"]}
    base = {f["key"]: f["value"] for f in idx["factors"]}
    wsum = sum(weights[k] for k, v in base.items() if v is not None) or 1

    def score(values):
        return sum(weights[k] * v for k, v in values.items() if v is not None) / wsum * 100

    target = GRADE_AT[gi + 1]
    old_kp = [[keyword_power(k) for k in c.get("keywords", [])] for c in checked]
    weeks_active = idx["stats"]["weeks_active"]

    flat_old = [kp for p in old_kp for kp in p]
    kp_now = sum(flat_old) / len(flat_old) if flat_old else 0

    def kp_of(vol, rank):
        return rank_score(rank) * clamp(math.log10(vol + 1) / 5)

    def keyword_with(n_new, new_kp):
        """새 글 n_new 개가 최근 글 목록 앞에 들어왔을 때 키워드 경쟁력.
        현실적으로 대표 태그만 제 순위에 뜨고, 두 번째는 절반, 세 번째는 못 뜬다고 본다."""
        posts = [[new_kp, new_kp * 0.5, 0.0] for _ in range(n_new)] + old_kp
        kws = [kp for i, p in enumerate(posts[:15]) for kp in (p[:3] if i < 10 else p[:1])]
        return clamp(2 * sum(kws) / len(kws)) if kws else base.get("keyword")

    def simulate(rate, new_kp, weeks):
        """주 rate 개씩, 대표 키워드 경쟁력 new_kp 인 글을 weeks 주 동안 쓰면."""
        w = min(weeks, 12)
        v = dict(base)
        v["pace"] = clamp((len(posts_12w) * (12 - w) / 12 + rate * w) / 12 / 7)
        v["consistency"] = clamp((weeks_active * (12 - w) / 12 + min(1, rate) * w) / 12)
        if v.get("keyword") is not None:
            v["keyword"] = keyword_with(int(rate * weeks), new_kp)
        return score(v)

    def weeks_to(rate, new_kp):
        for wk in range(1, 27):
            if simulate(rate, new_kp, wk) >= target:
                return wk
        return None

    scenarios = []
    for label, rate, new_kp in [
        ("지금처럼 (발행·키워드 그대로)", max(0.1, idx["stats"]["weekly_posts"]), kp_now),
        ("주 3개 · 대표 키워드 월 검색 1~3천, 10위 안", 3, kp_of(2000, 7)),
        ("주 5개 · 대표 키워드 월 검색 1~3천, 10위 안", 5, kp_of(2000, 7)),
        ("매일 1개 · 대표 키워드 월 검색 3~5천, 5위 안", 7, kp_of(4000, 4)),
    ]:
        scenarios.append({"label": label, "per_week": rate, "weeks": weeks_to(rate, new_kp),
                          "after_4w": round(simulate(rate, new_kp, 4), 1),
                          "after_12w": round(simulate(rate, new_kp, 12), 1)})

    # 항목 하나만 바꿨을 때 오르는 점수
    levers = []
    for label, key, value, how in [
        ("발행 페이스", "pace", 3 / 7, "12주 동안 주 3개씩 쓰면"),
        ("꾸준함", "consistency", 1.0, "12주 동안 매주 1개 이상 쓰면"),
        ("통합검색 노출", "total", 0.8, "최근 글 80%가 통검에 뜨면"),
        ("방문자 규모", "visitors", None, None),
    ]:
        v = dict(base)
        if key == "visitors":
            if base.get("visitors") is None or not vis_avg:
                continue
            goal = round(vis_avg * 2)
            value, how = clamp(math.log10(goal + 1) / 4), f"하루 방문이 {round(vis_avg)}명 → {goal}명이 되면"
        if base.get(key) is None or base[key] >= value:
            continue
        v[key] = value
        levers.append({"label": label, "how": how, "gain": round(score(v) - idx["score"], 1)})
    if base.get("keyword") is not None:
        levers.append({"label": "키워드 경쟁력", "how": "새 글 5개를 월 검색 2천 키워드 10위 안에 띄우면",
                       "gain": round(score({**base, "keyword": keyword_with(5, kp_of(2000, 7))}) - idx["score"], 1)})
    levers.sort(key=lambda x: -x["gain"])
    return {
        "next_grade": GRADES[gi + 1],
        "next_at": target,
        "need": round(max(0, target - idx["score"]), 1),
        "this_at": GRADE_AT[gi],
        "scenarios": scenarios,
        "levers": levers,
    }


# ---------------------------------------------------------------- 경쟁 블로그

def avg_visitors(visitors):
    """5일 방문자 중 집계 중인 오늘을 뺀 평균."""
    days = visitors[:-1] if visitors and len(visitors) > 1 else visitors
    return round(sum(v["count"] for v in days) / len(days), 1) if days else None


def recent_posts(blog_id, days=30):
    """총 글 수와 최근 N일 글 수 (글 목록 첫 페이지만)."""
    url = ("https://blog.naver.com/PostTitleListAsync.naver?"
           + urllib.parse.urlencode({"blogId": blog_id, "viewdate": "", "currentPage": 1,
                                     "categoryNo": 0, "parentCategoryNo": "", "countPerPage": 30}))
    try:
        _, body = http_get(url, {"Referer": f"https://blog.naver.com/{blog_id}"})
        data = json.loads(body.replace("\\'", "'"))
    except NET_ERRORS:
        return None, None
    if data.get("resultCode") != "S":
        return None, None
    since = now_kst() - timedelta(days=days)
    rows = data.get("postList") or []
    return to_int(data.get("totalCount")), sum(1 for p in rows if parse_list_date(p.get("addDate")) >= since)


def weight_score(b):
    """블로그 체급 0~100: 방문자 50 · 구독자 20 · 최근 30일 발행 20 · 누적 글 10.
    경쟁 블로그도 똑같이 잴 수 있는 공개 지표만 쓴다."""
    vis, subs = b.get("visitors_avg"), b.get("subscribers") or 0
    if vis is None:  # 방문자 비공개면 오늘 방문자로 대신
        vis = b.get("today_visitors") or 0
    parts = [
        (50, clamp(math.log10(vis + 1) / 4)),
        (20, clamp(math.log10(subs + 1) / 4)),
        (20, clamp((b.get("posts_30d") or 0) / 30)),
        (10, clamp(math.log10((b.get("total_posts") or 0) + 1) / 3.5)),
    ]
    return round(sum(w * v for w, v in parts), 1)


def pick_competitors(seen, limit):
    """여러 키워드에서 겹칠수록, 순위가 높을수록 가까운 경쟁자."""
    ranked = sorted(seen.items(), key=lambda kv: (-len(kv[1]), sum(kv[1].values()) / len(kv[1])))
    return ranked[:limit]


def build_competitors(me, seen, limit=20):
    rows = []
    for bid, kws in pick_competitors(seen, limit):
        info = fetch_blog_info(bid)
        if not info:
            continue
        total, p30 = recent_posts(bid)
        vis = fetch_visitors(bid)
        b = {
            "blog_id": bid, "name": info.get("name"), "nickname": info.get("nickname"),
            "topic": info.get("topic"), "subscribers": info.get("subscribers"),
            "today_visitors": info.get("today_visitors"), "visitors_avg": avg_visitors(vis),
            "total_posts": total, "posts_30d": p30,
            "shared": [{"keyword": k, "rank": r} for k, r in sorted(kws.items(), key=lambda x: x[1])],
            "influencer": info.get("power_blog") or info.get("naver_mate"),
        }
        b["score"] = weight_score(b)
        rows.append(b)
        time.sleep(0.4)
    me = {**me, "score": weight_score(me), "me": True}
    everyone = sorted(rows + [me], key=lambda b: -b["score"])
    for i, b in enumerate(everyone, 1):
        b["rank"] = i

    def place(key):
        vals = sorted([b.get(key) or 0 for b in everyone], reverse=True)
        return vals.index(me.get(key) or 0) + 1

    n = len(everyone)
    return {
        "count": n,
        "my_rank": me["rank"],
        "top_percent": round(me["rank"] / n * 100) if n else None,
        "same_topic": sum(1 for b in rows if b.get("topic") and b["topic"] == me.get("topic")),
        "my_place": {"visitors_avg": place("visitors_avg"), "subscribers": place("subscribers"),
                     "posts_30d": place("posts_30d"), "total_posts": place("total_posts")},
        "averages": {k: round(sum(b.get(k) or 0 for b in rows) / len(rows), 1) if rows else None
                     for k in ("visitors_avg", "subscribers", "posts_30d", "total_posts", "score")},
        "blogs": everyone,
    }


# ---------------------------------------------------------------- 실행

def main():
    cfg = load_json(os.path.join(ROOT, "config.json"), {})
    blog_id = (os.environ.get("BLOG_ID") or cfg.get("blog_id") or "").strip()
    stamp = now_kst()
    if not blog_id:
        save_json(os.path.join(DATA_DIR, "latest.json"), {"configured": False, "updated_at": stamp.isoformat()})
        print("config.json 의 blog_id 가 비어 있습니다. 아이디를 넣고 다시 실행하세요.")
        return 0

    max_check = int(cfg.get("max_check_posts", 15))
    kw_posts = int(cfg.get("keyword_check_posts", 10))
    print(f"[{blog_id}] 수집 시작")
    profile, rss = fetch_rss(blog_id)
    info = fetch_blog_info(blog_id)
    total, plist = fetch_post_list(blog_id)
    visitors = fetch_visitors(blog_id)
    post_stats = fetch_post_stats(blog_id)
    stats_by_no = {p["logNo"]: p for p in post_stats}
    print(f"공감·댓글 수집 {len(post_stats)}개 글")
    posts_12w = [p for p in plist if datetime.fromisoformat(p["date"]) >= stamp - timedelta(weeks=12)]

    search = Search(blog_id)
    print(f"검색 모드: {search.mode}")
    checked = []
    for n, p in enumerate(plist[:max_check]):
        tags = rss.get(p["logNo"], {}).get("tags", [])
        item = {
            "logNo": p["logNo"], "title": p["title"],
            "url": f"https://blog.naver.com/{blog_id}/{p['logNo']}",
            "date": p["date"], "comments": p["comments"],
            "likes": stats_by_no.get(p["logNo"], {}).get("likes"),
            "category": rss.get(p["logNo"], {}).get("category", ""), "tags": tags,
        }
        if p["searchable"]:
            item["status"], item["total_search"] = search.exposure(p["title"], p["logNo"])
        else:
            item["status"], item["total_search"] = "검색 비허용", None
        item["keywords"] = [{"keyword": t, **search.rank(t, p["logNo"])}
                            for t in tags[:3 if n < kw_posts else 1]]
        item["content"] = analyze_post(blog_id, p["logNo"], p["title"], tags[0] if tags else "")
        checked.append(item)
        print(f"  {item['status']}/{item['total_search']} {item['content'] and item['content']['score']}점 {p['title'][:30]}")

    # 검색이 막혀 실패한 글은 지난 수집 결과를 그대로 쓴다 (0점 처리 방지)
    prev = load_json(os.path.join(DATA_DIR, "latest.json"), {})
    prev_posts = {p["logNo"]: p for p in prev.get("posts", [])} if prev.get("blog_id") == blog_id else {}
    reused = 0
    for item in checked:
        old = prev_posts.get(item["logNo"])
        if not old:
            continue
        if item["status"] == "에러" and old.get("status") in ("노출", "누락"):
            item["status"], item["total_search"], reused = old["status"], old.get("total_search"), reused + 1
        elif item["total_search"] == "에러" and old.get("total_search") in ("노출", "미노출"):
            item["total_search"] = old["total_search"]
        old_kw = {k["keyword"]: k for k in old.get("keywords", []) if not k.get("error")}
        item["keywords"] = [old_kw.get(k["keyword"], k) if k.get("error") else k for k in item["keywords"]]
    if reused:
        print(f"검색 실패 {reused}개 글은 지난 결과 사용")

    watch = [{"keyword": kw, **search.rank(kw)} for kw in cfg.get("keywords", [])]

    # 키워드 검색량 (검색광고 API 키가 있을 때)
    tool = KeywordTool()
    all_kw = [k["keyword"] for c in checked for k in c["keywords"]] + [w["keyword"] for w in watch]
    kv_path = os.path.join(DATA_DIR, "keyword_volumes.json")
    kv_cache = load_json(kv_path, {})
    vols = tool.volumes(all_kw, kv_cache)
    save_json(kv_path, kv_cache)
    for k in [k for c in checked for k in c["keywords"]] + watch:
        k["volume"] = vols.get(k["keyword"].replace(" ", ""))
        k["docs"] = search.doc_counts.get(k["keyword"])

    # 방문자: 매 수집의 5일치를 합쳐 긴 기록으로
    vpath = os.path.join(DATA_DIR, "visitors.json")
    vmap = {v["date"]: v["count"] for v in load_json(vpath, []) if v.get("blog_id", blog_id) == blog_id}
    for v in visitors or []:
        vmap[v["date"]] = v["count"]
    vseries = [{"date": d, "count": c} for d, c in sorted(vmap.items())][-400:]
    save_json(vpath, [{**v, "blog_id": blog_id} for v in vseries])

    print(f"경쟁 블로그 후보 {len(search.seen)}곳")
    competitors = build_competitors({
        "blog_id": blog_id, "name": info.get("name") or profile["title"], "nickname": info.get("nickname"),
        "topic": info.get("topic"), "subscribers": info.get("subscribers"),
        "today_visitors": info.get("today_visitors"), "visitors_avg": avg_visitors(visitors),
        "total_posts": total,
        "posts_30d": sum(1 for p in plist if datetime.fromisoformat(p["date"]) >= stamp - timedelta(days=30)),
        "shared": [],
    }, search.seen, int(cfg.get("competitors", 20)))
    print(f"경쟁 블로그 {competitors['count']}곳 중 {competitors['my_rank']}위")

    idx = estimate_index(posts_12w, checked, visitors, info)
    diag, risk = diagnose(idx, checked, posts_12w, vseries)
    idx["low_quality_risk"] = risk
    idx["plan"] = level_plan(idx, checked, posts_12w, avg_visitors(visitors))

    save_json(os.path.join(DATA_DIR, "latest.json"), {
        "configured": True,
        "blog_id": blog_id,
        "updated_at": stamp.isoformat(),
        "search_mode": search.mode,
        "keyword_tool": tool.enabled,
        "search_calls": search.calls,
        "search_reused": reused,
        "profile": profile,
        "info": info,
        "total_posts": total,
        "last_post": plist[0]["date"] if plist else None,
        "posts_12w": len(posts_12w),
        "visitors": visitors,
        "index": idx,
        "diagnosis": diag,
        "posts": checked,
        "post_stats": post_stats,
        "watch_keywords": watch,
        "competitors": competitors,
    })

    hist_path = os.path.join(DATA_DIR, "history.json")
    today = stamp.strftime("%Y-%m-%d")
    history = [h for h in load_json(hist_path, []) if h.get("blog_id") == blog_id and h["date"] != today]
    history.append({
        "date": today, "blog_id": blog_id,
        "score": idx["score"], "grade": idx["grade"],
        "exposure_rate": idx["stats"]["exposure_rate"],
        "total_exposure_rate": idx["stats"]["total_exposure_rate"],
        "visitors_today": visitors[-1]["count"] if visitors else info.get("today_visitors"),
        "subscribers": info.get("subscribers"),
        "total_posts": total,
        "keywords": {w["keyword"]: w.get("rank") for w in watch},
        "competitor_rank": competitors["my_rank"],
        "competitor_count": competitors["count"],
    })
    save_json(hist_path, history[-400:])
    print(f"완료: {idx['grade']} ({idx['score']}점), 블로그탭 {idx['stats']['exposure_rate']}, "
          f"통검 {idx['stats']['total_exposure_rate']}, 검색 {search.calls}회")
    return 0


if __name__ == "__main__":
    sys.exit(main())
