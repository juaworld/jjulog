"""쭈로그 수집기 — 내 네이버 블로그 현황.

네이버 공개 데이터(RSS, 글 목록, 모바일 블로그 정보, 글 본문, 방문자 그래프, 검색)를 모아
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

    def _web(self, params):
        self.calls += 1
        time.sleep(1.2)
        _, text = http_get("https://search.naver.com/search.naver?" + urllib.parse.urlencode(params),
                           {"Referer": "https://www.naver.com"})
        return text

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

    def volumes(self, keywords):
        out = {}
        if not self.enabled:
            return out
        # 여러 개를 한 번에 물으면 일부가 "< 10" 으로 잘못 오는 경우가 있어 하나씩 묻는다
        for kw in dict.fromkeys(k.replace(" ", "") for k in keywords if k.strip()):
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
            row = next((r for r in json.loads(body).get("keywordList", [])
                        if r.get("relKeyword", "").upper() == kw.upper()), None)
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
    kws = [k for c in checked for k in c.get("keywords", []) if not k.get("error")]
    ranks = [k.get("rank") for k in kws]
    # 태그 절반은 원래 큰 키워드라 평균의 2배를 만점으로
    kw = clamp(2 * sum(keyword_power(k) for k in kws) / len(kws)) if kws else None
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

    watch = [{"keyword": kw, **search.rank(kw)} for kw in cfg.get("keywords", [])]

    # 키워드 검색량 (검색광고 API 키가 있을 때)
    tool = KeywordTool()
    all_kw = [k["keyword"] for c in checked for k in c["keywords"]] + [w["keyword"] for w in watch]
    vols = tool.volumes(all_kw)
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

    idx = estimate_index(posts_12w, checked, visitors, info)
    diag, risk = diagnose(idx, checked, posts_12w, vseries)
    idx["low_quality_risk"] = risk

    save_json(os.path.join(DATA_DIR, "latest.json"), {
        "configured": True,
        "blog_id": blog_id,
        "updated_at": stamp.isoformat(),
        "search_mode": search.mode,
        "keyword_tool": tool.enabled,
        "search_calls": search.calls,
        "profile": profile,
        "info": info,
        "total_posts": total,
        "last_post": plist[0]["date"] if plist else None,
        "posts_12w": len(posts_12w),
        "visitors": visitors,
        "index": idx,
        "diagnosis": diag,
        "posts": checked,
        "watch_keywords": watch,
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
    })
    save_json(hist_path, history[-400:])
    print(f"완료: {idx['grade']} ({idx['score']}점), 블로그탭 {idx['stats']['exposure_rate']}, "
          f"통검 {idx['stats']['total_exposure_rate']}, 검색 {search.calls}회")
    return 0


if __name__ == "__main__":
    sys.exit(main())
