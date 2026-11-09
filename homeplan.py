"""홈판 마스터 탭: 홈판용 / 검색용 / 쇼핑·여행 커넥트 / 스레드 글쓰기 워크플로우.

app.py의 call_gemini 같은 의존성은 render_homeplan_master()의 인자로 주입받는다
(app.py를 다시 import하면 스크립트가 두 번 실행되므로 순환 import를 피하기 위함).

이 모듈은 '초안 생성 + 복사/다운로드'까지만 담당한다. 네이버 에디터 자동 입력,
다중 계정 자동 로그인, 스레드 자동 게시처럼 플랫폼 약관을 우회하는 자동화나,
타인 사진의 워터마크 제거/중복 회피 가공 같은 기능은 의도적으로 넣지 않았다.
"""

import html
import json
import re
from urllib.parse import quote_plus

import feedparser
import streamlit as st

GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
MAX_SOURCE_CANDIDATES = 10

LENGTH_OPTIONS = {
    "짧게 (약 1,500자)": 1500,
    "보통 (약 2,000자)": 2000,
    "길게 (약 2,500자)": 2500,
    "아주 길게 (약 3,000자)": 3000,
}
STRUCTURE_OPTIONS = {
    "home": ["스토리텔링형 (경험·감정 중심)", "이슈 정리형 (무슨 일·왜·앞으로)", "리스트형 (포인트 N가지)", "Q&A형"],
    "search": ["정보 정리형 (개념·방법·주의점)", "Q&A형", "리스트형 (포인트 N가지)", "비교 분석형"],
    "connect": ["솔직 리뷰형", "문제 해결형 (고민→해결)", "추천 리스트형", "비교형 (다른 선택지와 비교)"],
}
TONE_OPTIONS = [
    "친근한 해요체",
    "담백한 합니다체",
    "수다스러운 구어체 (~했어요, ~더라고요)",
    "일기형 음슴체 (~했음, ~임)",
]
THUMB_COLORS = {
    "네이비": ("#1f2a44", "#3b4f7d"),
    "블랙": ("#111111", "#3a3a3a"),
    "레드": ("#8e1b1b", "#d1453b"),
    "그린": ("#0f4d3a", "#2f8f6b"),
    "퍼플": ("#3d1f5c", "#7b4bb0"),
}

CONNECT_DISCLOSURE = {
    "쇼핑 커넥트": "※ 이 포스팅은 네이버 쇼핑 커넥트 활동의 일환으로, 판매 발생 시 일정 수수료를 제공받습니다.",
    "여행 커넥트": "※ 이 포스팅은 네이버 여행 커넥트 활동의 일환으로, 예약 발생 시 일정 수수료를 제공받습니다.",
}

MODE_GUIDE = {
    "home": """[홈판(블로그 홈 피드) 최적화 규칙]
- 홈판은 검색이 아니라 '피드에서 스크롤하다 멈추게 만드는 글'이 노출된다. 호기심·감정·공감을 자극하는 것이 최우선이다.
- 도입부 3줄 안에 '그래서 무슨 일이야?'라는 궁금증을 만들고, 결론은 바로 다 말하지 말고 한 단계씩 풀어간다.
- 모바일 가독성: 한 문단은 1~3문장, 문단 사이 빈 줄. 긴 설명은 짧은 문장으로 쪼갠다.
- 사실 정리에서 끝내지 말고, 글쓴이의 개인적인 느낌·생각·관점을 곳곳에 넣어 '사람이 쓴 글'로 만든다.
- 마지막은 독자에게 의견을 묻는 한 줄(댓글 유도)로 마무리한다.""",
    "search": """[검색 노출 최적화 규칙]
- 메인 키워드를 제목 앞쪽에 자연스럽게 넣고, 본문에는 메인 키워드를 4~6회, 연관 키워드를 2~3회 자연스럽게 섞는다.
- 도입부 2~3문장 안에 검색자가 원하는 핵심 답을 먼저 제시한다(두괄식).
- 소제목은 검색자가 실제로 궁금해할 질문형 문장으로 쓰고, 소제목 바로 아래에서 즉답한다.
- 수치·기준·방법·주의사항 등 구체적인 정보 위주로 쓰고, 모호한 표현을 피한다.
- 본문 하단에 자주 묻는 질문 2~3개를 Q&A 형태로 넣는다.""",
}

COMMON_WRITING_RULES = """[공통 작성 규칙]
- 참고 자료의 문장을 그대로 옮기거나 짜깁기하지 마라. 사람들이 '어디에 반응했는지(쟁점, 감정, 궁금증)'만 참고하고, 표현·구성·관점은 완전히 새로 써라.
- 참고 자료에 없는 사실(수치, 날짜, 인용문, 실명 발언)을 지어내지 마라. 확인되지 않은 내용은 '~라고 알려졌다', '~로 보인다'처럼 표현한다.
- 특정 개인을 비방하거나 단정적으로 매도하지 마라.
- 마크다운 표(|...|)와 샵(#) 제목 기호는 쓰지 마라. 소제목은 앞에 '▶'를 붙인 한 줄로 쓴다. 강조는 **굵게**만 사용한다.
- 사진이 들어가면 좋을 자리에 [사진: 어떤 사진인지 짧은 설명] 형태로 표시하라. 본문 전체에 3~5개.
- 분량은 공백 포함 약 {length}자(±10%)로 맞춘다."""

DRAFT_OUTPUT_FORMAT = """[출력 형식] 아래 형식과 머리표를 정확히 지켜라. 다른 말은 덧붙이지 마라.
[제목후보]
1. (제목)
2. (제목)
3. (제목)
4. (제목)
5. (제목)
[썸네일]
메인: (썸네일에 크게 들어갈 문구, 공백 포함 18자 이내)
서브: (썸네일 아래 작은 문구, 공백 포함 30자 이내)
[본문]
(본문 전체)
[태그]
(#태그 형식으로 5~10개, 한 줄에 공백으로 구분)"""


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text):
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _init(key, default):
    if key not in st.session_state:
        st.session_state[key] = default


def _set_state(key, value):
    st.session_state[key] = value


def parse_json_response(text):
    """모델이 ```json 코드펜스나 앞뒤 설명을 붙여도 JSON 본체만 뽑아 파싱한다."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("AI 응답을 해석하지 못했습니다. 다시 시도해 주세요.")


def parse_draft_sections(text):
    """[제목후보]/[썸네일]/[본문]/[태그] 머리표로 구분된 초안을 dict로 분리한다."""
    sections = {}
    current = None
    buf = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*\[(제목후보|썸네일|본문|태그)\]\s*$", line)
        if m:
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = m.group(1), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()

    titles = []
    for line in sections.get("제목후보", "").splitlines():
        t = re.sub(r"^\s*\d+[.)]\s*", "", line).strip()
        if t:
            titles.append(t)

    thumb_main, thumb_sub = "", ""
    for line in sections.get("썸네일", "").splitlines():
        if line.strip().startswith("메인:"):
            thumb_main = line.split(":", 1)[1].strip()
        elif line.strip().startswith("서브:"):
            thumb_sub = line.split(":", 1)[1].strip()

    body = sections.get("본문", "")
    if not body:
        # 머리표를 지키지 않은 응답이라도 버리지 않고 본문으로 사용한다.
        body = (text or "").strip()

    return {
        "titles": titles[:5],
        "thumb_main": thumb_main,
        "thumb_sub": thumb_sub,
        "body": body,
        "tags": sections.get("태그", "").strip(),
    }


def to_naver_plain(text):
    """네이버 에디터에 붙여넣기 좋은 평문으로 변환(마크다운 기호 제거)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_stats(title, body):
    plain = to_naver_plain(body)
    no_photo = re.sub(r"\[사진:[^\]]*\]", "", plain)
    return {
        "chars": len(no_photo),
        "chars_no_space": len(re.sub(r"\s", "", no_photo)),
        "photos": len(re.findall(r"\[사진:[^\]]*\]", plain)),
        "title_len": len(title or ""),
    }


def search_news(query, limit=MAX_SOURCE_CANDIDATES):
    feed = feedparser.parse(GOOGLE_NEWS_SEARCH_URL.format(query=quote_plus(query)))
    results = []
    seen = set()
    for entry in feed.entries:
        title = _clean(entry.get("title", ""))
        if not title or title in seen:
            continue
        seen.add(title)
        source = ""
        src = entry.get("source")
        if isinstance(src, dict):
            source = src.get("title", "")
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        summary = _clean(entry.get("summary", ""))
        if summary.startswith(title):
            summary = summary[len(title) :].strip()
        results.append(
            {
                "title": title,
                "source": source,
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": summary[:300],
            }
        )
        if len(results) >= limit:
            break
    return results


def _wrap_chars(text, width):
    lines, line = [], ""
    for word in text.split(" "):
        candidate = f"{line} {word}".strip()
        if len(candidate) <= width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
        while len(line) > width:
            lines.append(line[:width])
            line = line[width:]
    if line:
        lines.append(line)
    return lines


def build_thumbnail_svg(main, sub, color_name):
    c1, c2 = THUMB_COLORS.get(color_name, THUMB_COLORS["네이비"])
    main_lines = _wrap_chars(main or "", 8)[:4]
    sub_lines = _wrap_chars(sub or "", 16)[:2]
    size = 800
    main_font = 96
    line_gap = 116
    block_h = len(main_lines) * line_gap + (len(sub_lines) * 52 + 40 if sub_lines else 0)
    y = (size - block_h) / 2 + main_font * 0.85
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">',
        "<defs><linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">"
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>',
        f'<rect width="{size}" height="{size}" fill="url(#g)"/>',
        f'<rect x="40" y="40" width="{size - 80}" height="{size - 80}" fill="none" stroke="#ffffff" stroke-opacity="0.35" stroke-width="4"/>',
    ]
    font = "'Noto Sans KR','Apple SD Gothic Neo','Malgun Gothic',sans-serif"
    for line in main_lines:
        parts.append(
            f'<text x="{size / 2}" y="{y:.0f}" text-anchor="middle" font-family="{font}" '
            f'font-size="{main_font}" font-weight="900" fill="#ffffff">{html.escape(line)}</text>'
        )
        y += line_gap
    if sub_lines:
        y += 10
        for line in sub_lines:
            parts.append(
                f'<text x="{size / 2}" y="{y:.0f}" text-anchor="middle" font-family="{font}" '
                f'font-size="42" font-weight="500" fill="#ffe27a">{html.escape(line)}</text>'
            )
            y += 52
    parts.append("</svg>")
    return "".join(parts)


def _run(label, fn):
    """AI 호출을 스피너와 에러 표시로 감싼다. 실패하면 None."""
    try:
        with st.spinner(label):
            return fn()
    except Exception as e:  # noqa: BLE001 - 사용자에게 원인을 그대로 보여준다
        st.error(str(e))
        return None


def _library_label(i, item):
    return f"{i + 1}. [{item['kind']}] {item['title']}"


def _add_to_library(kind, title, body, link=""):
    library = st.session_state.setdefault("hp_library", [])
    library.append({"kind": kind, "title": title, "body": body, "link": link})
    # 스레드 탭의 원본 선택 박스가 방금 보낸 글을 바로 가리키도록 한다.
    st.session_state["hp_threads_src"] = _library_label(len(library) - 1, library[-1])
    st.toast(f"'{title[:20]}…' 글을 스레드 탭으로 보냈습니다.")


# ---------------------------------------------------------------------------
# 프롬프트
# ---------------------------------------------------------------------------


def _sources_block(sources):
    if not sources:
        return "(선택된 참고 자료 없음 — 주제에 대한 일반적인 관심사를 바탕으로 작성)"
    lines = []
    for i, s in enumerate(sources, 1):
        lines.append(f"{i}. [{s.get('source') or '출처 미상'}] {s['title']}\n   요약: {s.get('summary') or '-'}")
    return "\n".join(lines)


def prompt_topic_ideas(mode, seed):
    if mode == "search":
        task = (
            f"키워드 '{seed}'로 네이버 검색 유입을 노릴 수 있는 정보성 롱테일 키워드 8개를 추천하라. "
            "검색 의도가 분명하고 대형 블로그와 경쟁이 덜한 질문형/해결형 키워드 위주로."
        )
    else:
        task = (
            f"키워드 '{seed}'와 관련해 네이버 블로그 홈판(피드)에서 사람들이 클릭할 만한 글감 8개를 추천하라. "
            "최근 화제성·호기심·공감을 자극하는 구체적인 소재 위주로."
        )
    return (
        f"{task}\n"
        'JSON으로만 답하라: {"ideas":[{"keyword":"글감 또는 키워드","reason":"추천 이유 한 줄"}]}'
    )


def prompt_angles(mode, topic, sources):
    target = "블로그 홈판(피드)에서 클릭을 부르는" if mode == "home" else "네이버 검색 상위 노출을 노리는"
    return f"""주제: {topic}

[참고 자료 — 사람들이 반응한 지점 파악용]
{_sources_block(sources)}

위 주제로 {target} 블로그 글을 쓰려 한다. 서로 겹치지 않는 '글의 관점' 3가지를 제안하라.
같은 주제라도 관점이 다르면 완전히 다른 글이 되도록, 각각 다른 궁금증/감정/정보 욕구를 겨냥하라.
참고 자료에 없는 사실을 지어내지 마라.

JSON으로만 답하라:
{{"angles":[{{"angle":"관점을 한 문장으로","hook":"독자가 멈춰서 읽게 만드는 포인트","outline":["소제목1","소제목2","소제목3"]}}]}}"""


def prompt_draft(mode, topic, sources, angle, length, structure, tone):
    outline = " / ".join(angle.get("outline", []))
    return f"""너는 네이버 블로그 상위 노출 글을 꾸준히 써 온 블로거다.

주제: {topic}
이번 글의 관점: {angle.get('angle', '')}
독자를 붙잡을 포인트: {angle.get('hook', '')}
참고 목차: {outline or '자유'}
글 구성: {structure}
문체: {tone}

[참고 자료 — 반응 포인트만 참고하고 문장은 절대 옮기지 말 것]
{_sources_block(sources)}

{MODE_GUIDE[mode]}

{COMMON_WRITING_RULES.format(length=length)}

[제목 규칙]
- 5개 모두 서로 다른 스타일(궁금증 유발형, 숫자형, 감정형, 정보형, 반전형)로 쓴다.
- 공백 포함 20~35자. 낚시성 허위 표현은 금지.

{DRAFT_OUTPUT_FORMAT}"""


def prompt_extract_product(kind, page_text):
    return f"""아래는 네이버 {kind} 상품(또는 숙소·투어) 상세 페이지에서 복사한 텍스트다.
블로그 글 작성에 필요한 정보만 정리하라. 텍스트에 없는 정보는 빈 문자열로 둬라. 지어내지 마라.

[페이지 텍스트]
{page_text[:8000]}

JSON으로만 답하라:
{{"name":"상품명","price":"판매가","discount":"할인율 또는 할인 정보","seller":"판매처/브랜드","features":["핵심 특징1","핵심 특징2","핵심 특징3"],"target":"이 상품이 잘 맞는 사람","cautions":"구매 전 확인할 점"}}"""


def prompt_connect_draft(kind, product, length, structure, tone):
    features = "\n".join(f"- {f}" for f in product.get("features", []) if f)
    return f"""너는 네이버 블로그에서 {kind} 상품을 소개하는 블로거다.

[상품 정보]
- 상품명: {product.get('name', '')}
- 가격: {product.get('price', '')}
- 할인: {product.get('discount', '')}
- 판매처: {product.get('seller', '')}
- 특징:
{features or '- (정보 없음)'}
- 잘 맞는 사람: {product.get('target', '')}
- 확인할 점: {product.get('cautions', '')}

글 구성: {structure}
문체: {tone}

[작성 규칙]
- 상품 정보에 없는 효능·성능·수치·후기를 지어내지 마라. 직접 써 본 것처럼 단정하는 가짜 사용 후기는 금지. 대신 '이런 분께 맞겠다', '이 점이 눈에 띈다'처럼 소개자 시점으로 쓴다.
- 건강기능식품·의료기기·화장품이면 질병 치료·예방 효과를 암시하는 표현을 쓰지 마라.
- 독자의 고민/상황으로 시작해 자연스럽게 상품으로 연결한다. 장점만 나열하지 말고 아쉬운 점이나 확인할 점도 1~2개 솔직하게 쓴다.
- 구매 링크가 들어갈 자리에 [링크] 라고 단독 줄로 2~3번 표시하라(중간 1회, 마지막 1회 이상).
- 마크다운 표와 샵(#) 기호는 쓰지 마라. 소제목은 앞에 '▶'를 붙인 한 줄로 쓴다. 강조는 **굵게**만 사용한다.
- 사진이 들어가면 좋을 자리에 [사진: 어떤 사진인지 짧은 설명] 형태로 3~5개 표시하라.
- 분량은 공백 포함 약 {length}자(±10%).
- 수수료 고지 문구는 시스템이 자동으로 넣으므로 본문에 쓰지 마라.

[제목 규칙]
- 5개 모두 서로 다른 스타일. 공백 포함 20~35자. 과장·허위 금지.

{DRAFT_OUTPUT_FORMAT}"""


def prompt_threads(source_title, source_body, counts, tone, spacing):
    spacing_rule = "문장마다 줄바꿈하고 문단 사이 빈 줄을 넣어 여유 있게" if spacing == "여유 있게" else "2~3문장씩 묶어 촘촘하게"
    return f"""너는 스레드(Threads)에서 팔로워와 소통하며 블로그 글로 자연스럽게 유입을 만드는 운영자다.

[원본 블로그 글]
제목: {source_title}
{source_body[:6000]}

아래 개수만큼 스레드 게시글을 써라.
- 정보형 {counts['정보형']}개: 원본 글의 핵심 정보 1가지를 짧게 알려주는 글. 링크 언급 없음.
- 일상형 {counts['일상형']}개: 이 주제와 관련된 운영자의 소소한 생각·경험담처럼 읽히는 글. 링크 언급 없음.
- 홍보형 {counts['홍보형']}개: 궁금증을 남기고 '자세한 내용은 블로그에 정리해 뒀다'로 이어지는 글. 링크 자체는 쓰지 말고 시스템이 붙인다.

[규칙]
- 말투: {tone}
- 줄바꿈: {spacing_rule}
- 각 글은 공백 포함 80~250자. 해시태그는 0~2개.
- 글마다 첫 문장을 다르게 시작하고, 같은 표현을 반복하지 마라.
- 원본에 없는 사실을 지어내지 마라.

JSON으로만 답하라: {{"posts":[{{"type":"정보형|일상형|홍보형","text":"게시글"}}]}}"""


# ---------------------------------------------------------------------------
# 홈판 / 검색 글쓰기
# ---------------------------------------------------------------------------


def _render_topic_ideas(mode, call_llm, topic_key):
    ideas_key = f"hp_{mode}_ideas"
    _init(ideas_key, [])
    seed_label = "검색 키워드 확장" if mode == "search" else "AI 글감 확장"
    col_seed, col_btn = st.columns([5, 1.4], gap="small")
    with col_seed:
        seed = st.text_input(
            "확장할 키워드",
            key=f"hp_{mode}_seed",
            placeholder="예: 크리에이터 어드바이저에서 찾은 이슈 키워드",
            label_visibility="collapsed",
        )
    with col_btn:
        if st.button(seed_label, key=f"hp_{mode}_seed_btn", use_container_width=True):
            if not seed.strip():
                st.warning("확장할 키워드를 입력해 주세요.")
            else:
                data = _run("키워드를 확장하는 중...", lambda: parse_json_response(call_llm(prompt_topic_ideas(mode, seed.strip()), json_mode=True)))
                if data:
                    st.session_state[ideas_key] = data.get("ideas", [])[:8]

    ideas = st.session_state[ideas_key]
    if ideas:
        with st.container(border=True):
            for i, idea in enumerate(ideas):
                c1, c2 = st.columns([6, 1.2], gap="small")
                c1.markdown(f"**{idea.get('keyword', '')}**  \n<small>{html.escape(idea.get('reason', ''))}</small>", unsafe_allow_html=True)
                c2.button(
                    "이걸로 쓰기",
                    key=f"hp_{mode}_idea_{i}",
                    on_click=_set_state,
                    args=(topic_key, idea.get("keyword", "")),
                )


def _render_trending_cards(mode, get_trending, topic_key):
    shown_key = f"hp_{mode}_trend_shown"
    _init(shown_key, False)
    if st.button("🔄 실시간 이슈 불러오기", key=f"hp_{mode}_trend_btn"):
        st.session_state[shown_key] = True
    if not st.session_state[shown_key]:
        return
    try:
        headlines = get_trending()
    except Exception as e:  # noqa: BLE001
        st.error(f"이슈를 불러오지 못했습니다: {e}")
        return
    if not headlines:
        st.info("불러온 이슈가 없습니다.")
        return
    cols = st.columns(2, gap="small")
    for i, headline in enumerate(headlines):
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(f"**{headline}**")
                st.button(
                    "이 글감으로 쓰기",
                    key=f"hp_{mode}_trend_use_{i}",
                    type="primary",
                    on_click=_set_state,
                    args=(topic_key, headline),
                )


def render_writing_flow(mode, call_llm, get_trending=None):
    p = f"hp_{mode}"
    topic_key = f"{p}_topic"
    for key, default in (
        (topic_key, ""),
        (f"{p}_sources", []),
        (f"{p}_src_nonce", 0),
        (f"{p}_angles", []),
        (f"{p}_draft", None),
        (f"{p}_draft_nonce", 0),
    ):
        _init(key, default)

    # 1. 글감 -------------------------------------------------------------
    st.markdown("#### ① 글감 찾기")
    if mode == "home" and get_trending is not None:
        _render_trending_cards(mode, get_trending, topic_key)
    _render_topic_ideas(mode, call_llm, topic_key)

    st.text_input(
        "글감(주제)" if mode == "home" else "메인 키워드",
        key=topic_key,
        placeholder="위에서 고르거나 직접 입력하세요",
    )
    topic = st.session_state[topic_key].strip()

    # 2. 관련 기사 후보 -------------------------------------------------------
    st.markdown("#### ② 관련 기사 후보")
    if st.button("관련 기사 후보 찾기", key=f"{p}_src_btn", disabled=not topic):
        results = _run("관련 기사를 찾는 중...", lambda: search_news(topic))
        if results is not None:
            st.session_state[f"{p}_sources"] = results
            st.session_state[f"{p}_src_nonce"] += 1
            st.session_state[f"{p}_angles"] = []
            if not results:
                st.info("관련 기사를 찾지 못했습니다. 참고 자료 없이 진행할 수도 있습니다.")

    sources = st.session_state[f"{p}_sources"]
    nonce = st.session_state[f"{p}_src_nonce"]
    selected = []
    if sources:
        st.caption("글 작성 시 참고할 출처를 체크하세요. (문장을 가져오지 않고, 사람들이 반응한 쟁점만 참고합니다)")
        with st.container(border=True):
            for i, s in enumerate(sources):
                label = f"**{s['title']}**  ·  {s.get('source') or ''}"
                c1, c2 = st.columns([8, 1], gap="small")
                with c1:
                    if st.checkbox(label, key=f"{p}_src_{nonce}_{i}", value=i < 3):
                        selected.append(s)
                with c2:
                    if s.get("link"):
                        st.link_button("원문", s["link"])

    # 3. 관점 추천 ----------------------------------------------------------
    st.markdown("#### ③ 관점 추천")
    if st.button("관점 3가지 추천받기", key=f"{p}_angle_btn", disabled=not topic):
        data = _run(
            "글의 관점을 고르는 중...",
            lambda: parse_json_response(call_llm(prompt_angles(mode, topic, selected), json_mode=True)),
        )
        if data:
            st.session_state[f"{p}_angles"] = data.get("angles", [])[:3]

    angles = st.session_state[f"{p}_angles"]
    angle = None
    if angles:
        labels = [f"{i + 1}. {a.get('angle', '')}" for i, a in enumerate(angles)]
        choice = st.radio("어떤 관점으로 쓸까요?", labels, key=f"{p}_angle_choice")
        angle = angles[labels.index(choice)] if choice in labels else angles[0]
        with st.container(border=True):
            st.markdown(f"🎯 **포인트:** {angle.get('hook', '')}")
            if angle.get("outline"):
                st.markdown("\n".join(f"- {o}" for o in angle["outline"]))

    # 4. 옵션 + 초안 -----------------------------------------------------------
    st.markdown("#### ④ 글 옵션 · 초안 만들기")
    c1, c2, c3 = st.columns(3, gap="small")
    length_label = c1.selectbox("글자수", list(LENGTH_OPTIONS), index=1, key=f"{p}_len")
    structure = c2.selectbox("글 구성", STRUCTURE_OPTIONS[mode], key=f"{p}_struct")
    tone = c3.selectbox("문체", TONE_OPTIONS, key=f"{p}_tone")

    if st.button("이 관점으로 초안 만들기", key=f"{p}_draft_btn", type="primary", disabled=not (topic and angle)):
        text = _run(
            "초안을 작성하는 중... (30초~1분)",
            lambda: call_llm(prompt_draft(mode, topic, selected, angle, LENGTH_OPTIONS[length_label], structure, tone)),
        )
        if text:
            st.session_state[f"{p}_draft"] = parse_draft_sections(text)
            st.session_state[f"{p}_draft_nonce"] += 1
    if not angle:
        st.caption("관점을 먼저 추천받아야 초안을 만들 수 있습니다.")

    draft = st.session_state[f"{p}_draft"]
    if draft:
        render_draft_result(p, draft, kind="홈판" if mode == "home" else "검색")


def render_draft_result(p, draft, kind, link="", disclosure=""):
    st.markdown("#### ⑤ 결과 확인 · 수정")
    nonce = st.session_state.get(f"{p}_draft_nonce", 0)
    titles = draft["titles"] or ["(제목 후보 없음)"]
    custom = "✏️ 직접 입력"
    title_choice = st.radio("제목 선택", titles + [custom], key=f"{p}_title_{nonce}")
    if title_choice == custom:
        title = st.text_input("내 제목", key=f"{p}_title_custom_{nonce}", value=titles[0])
    else:
        title = title_choice

    body = st.text_area("본문 (직접 고칠 수 있어요)", value=draft["body"], height=520, key=f"{p}_body_{nonce}")

    stats = text_stats(title, body)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("본문 글자수", f"{stats['chars']:,}자")
    m2.metric("공백 제외", f"{stats['chars_no_space']:,}자")
    m3.metric("사진 자리", f"{stats['photos']}개")
    m4.metric("제목 글자수", f"{stats['title_len']}자")

    # 썸네일
    with st.expander("🖼️ 썸네일 만들기", expanded=False):
        t1, t2, t3 = st.columns([3, 3, 1.4], gap="small")
        thumb_main = t1.text_input("메인 문구", value=draft.get("thumb_main") or title[:18], key=f"{p}_tm_{nonce}")
        thumb_sub = t2.text_input("서브 문구", value=draft.get("thumb_sub", ""), key=f"{p}_ts_{nonce}")
        color = t3.selectbox("색상", list(THUMB_COLORS), key=f"{p}_tc_{nonce}")
        svg = build_thumbnail_svg(thumb_main, thumb_sub, color)
        preview = svg.replace("<svg ", '<svg style="width:100%;height:auto" ', 1)
        st.html(f'<div style="max-width:360px">{preview}</div>')
        st.download_button("썸네일 SVG 다운로드", svg, file_name="thumbnail.svg", mime="image/svg+xml", key=f"{p}_tdl_{nonce}")
        st.caption("SVG는 브라우저로 열어 캡처하거나, 무료 변환기로 PNG로 바꿔 업로드하세요.")

    # 네이버 붙여넣기용
    plain_body = to_naver_plain(body)
    if link:
        plain_body = plain_body.replace("[링크]", link)
    naver_text = "\n\n".join(x for x in (disclosure, plain_body) if x)
    if draft.get("tags"):
        naver_text += f"\n\n{draft['tags']}"

    st.markdown("**네이버 에디터 붙여넣기용** — 제목과 본문을 각각 복사해 붙여넣고, `[사진: …]` 자리에 직접 찍거나 저작권이 확인된 사진을 넣으세요.")
    st.code(title, language=None, wrap_lines=True)
    st.code(naver_text, language=None, wrap_lines=True, height=400)

    st.button(
        "🧵 이 글을 스레드 탭으로 보내기",
        key=f"{p}_to_threads_{nonce}",
        on_click=_add_to_library,
        args=(kind, title, plain_body, link),
    )


# ---------------------------------------------------------------------------
# 쇼핑 / 여행 커넥트
# ---------------------------------------------------------------------------


def render_connect_flow(call_llm):
    p = "hp_connect"
    fields = ("name", "price", "discount", "seller", "features", "target", "cautions")
    for f in fields:
        _init(f"{p}_{f}", "")
    _init(f"{p}_draft", None)
    _init(f"{p}_draft_nonce", 0)

    kind = st.radio("유형", list(CONNECT_DISCLOSURE), horizontal=True, key=f"{p}_kind")

    st.markdown("#### ① 상품 정보 가져오기")
    st.caption(
        "브랜드 커넥트에서 홍보할 상품의 상세 페이지를 열고, 페이지 내용을 전체 선택(Ctrl+A) → 복사해 아래에 붙여넣으세요."
    )
    page_text = st.text_area("상세 페이지 텍스트", height=160, key=f"{p}_page")
    if st.button("AI로 상품 정보 정리", key=f"{p}_extract", disabled=not page_text.strip()):
        data = _run(
            "상품 정보를 정리하는 중...",
            lambda: parse_json_response(call_llm(prompt_extract_product(kind, page_text), json_mode=True)),
        )
        if data:
            for f in fields:
                value = data.get(f, "")
                if isinstance(value, list):
                    value = "\n".join(str(v) for v in value if v)
                st.session_state[f"{p}_{f}"] = str(value or "")

    with st.container(border=True):
        c1, c2 = st.columns(2, gap="small")
        c1.text_input("상품명", key=f"{p}_name")
        c2.text_input("판매처/브랜드", key=f"{p}_seller")
        c3, c4 = st.columns(2, gap="small")
        c3.text_input("가격", key=f"{p}_price")
        c4.text_input("할인 정보", key=f"{p}_discount")
        st.text_area("핵심 특징 (한 줄에 하나)", key=f"{p}_features", height=100)
        c5, c6 = st.columns(2, gap="small")
        c5.text_input("잘 맞는 사람", key=f"{p}_target")
        c6.text_input("구매 전 확인할 점", key=f"{p}_cautions")
        link = st.text_input("넣을 내 링크 (커넥트 링크)", key=f"{p}_link", placeholder="https://naver.me/...")

    st.markdown("#### ② 글 옵션 · 초안 만들기")
    o1, o2, o3 = st.columns(3, gap="small")
    length_label = o1.selectbox("글자수", list(LENGTH_OPTIONS), index=1, key=f"{p}_len")
    structure = o2.selectbox("글 구성", STRUCTURE_OPTIONS["connect"], key=f"{p}_struct")
    tone = o3.selectbox("문체", TONE_OPTIONS, key=f"{p}_tone")

    name = st.session_state[f"{p}_name"].strip()
    if st.button("초안 만들기", key=f"{p}_draft_btn", type="primary", disabled=not name):
        product = {f: st.session_state[f"{p}_{f}"] for f in fields}
        product["features"] = [x.strip() for x in product["features"].splitlines() if x.strip()]
        text = _run(
            "초안을 작성하는 중... (30초~1분)",
            lambda: call_llm(prompt_connect_draft(kind, product, LENGTH_OPTIONS[length_label], structure, tone)),
        )
        if text:
            st.session_state[f"{p}_draft"] = parse_draft_sections(text)
            st.session_state[f"{p}_draft_nonce"] += 1
    if not name:
        st.caption("상품명을 입력하면 초안을 만들 수 있습니다.")
    if not link.strip():
        st.caption("링크를 비워두면 본문의 [링크] 자리가 그대로 남습니다.")

    draft = st.session_state[f"{p}_draft"]
    if draft:
        render_draft_result(p, draft, kind=kind, link=link.strip(), disclosure=CONNECT_DISCLOSURE[kind])


# ---------------------------------------------------------------------------
# 스레드
# ---------------------------------------------------------------------------


def render_threads_flow(call_llm):
    p = "hp_threads"
    _init("hp_library", [])
    _init(f"{p}_posts", [])

    st.caption(
        "블로그 글을 바탕으로 정보형·일상형·홍보형 스레드 글을 섞어 만들어 드립니다. "
        "자동 반복 게시는 스레드 정책상 계정 제한 위험이 있어, 만든 글은 직접 복사해 올리세요."
    )

    library = st.session_state["hp_library"]
    options = ["직접 붙여넣기"] + [_library_label(i, x) for i, x in enumerate(library)]
    if st.session_state.get(f"{p}_src") not in options:
        st.session_state[f"{p}_src"] = options[-1]
    choice = st.selectbox("원본 블로그 글", options, key=f"{p}_src")
    if choice == "직접 붙여넣기":
        src_title = st.text_input("글 제목", key=f"{p}_title")
        src_body = st.text_area("글 본문", height=200, key=f"{p}_body")
        default_link = ""
    else:
        item = library[options.index(choice) - 1]
        src_title, src_body, default_link = item["title"], item["body"], item.get("link", "")
        with st.expander("원본 미리보기"):
            st.markdown(f"**{src_title}**")
            st.text(src_body[:1500])
    blog_url = st.text_input("블로그 글 주소 (홍보형에 붙일 링크)", value=default_link, key=f"{p}_url_{choice}")

    st.markdown("#### 글 비율")
    n1, n2, n3 = st.columns(3)
    counts = {
        "정보형": n1.number_input("정보형", 0, 10, 2, key=f"{p}_n_info"),
        "일상형": n2.number_input("일상형", 0, 10, 1, key=f"{p}_n_daily"),
        "홍보형": n3.number_input("홍보형 (링크 포함)", 0, 10, 2, key=f"{p}_n_promo"),
    }
    s1, s2, s3 = st.columns(3)
    tone = s1.selectbox("말투", ["친근한 반말", "다정한 존댓말", "짧고 툭툭 던지는 말투"], key=f"{p}_tone")
    link_pos = s2.selectbox("링크 위치", ["본문 마지막", "첫 댓글로 분리"], key=f"{p}_linkpos")
    spacing = s3.selectbox("줄바꿈", ["여유 있게", "촘촘하게"], key=f"{p}_spacing")

    total = sum(counts.values())
    if st.button("스레드 글 만들기", key=f"{p}_gen", type="primary", disabled=not (src_body.strip() and total)):
        data = _run(
            "스레드 글을 만드는 중...",
            lambda: parse_json_response(call_llm(prompt_threads(src_title, src_body, counts, tone, spacing), json_mode=True)),
        )
        if data:
            st.session_state[f"{p}_posts"] = data.get("posts", [])

    posts = st.session_state[f"{p}_posts"]
    if posts:
        st.markdown(f"#### 생성된 글 {len(posts)}개")
        url = blog_url.strip()
        for i, post in enumerate(posts, 1):
            ptype = post.get("type", "")
            text = post.get("text", "").strip()
            comment = ""
            if "홍보" in ptype and url:
                if link_pos == "본문 마지막":
                    text = f"{text}\n\n👉 {url}"
                else:
                    comment = f"자세한 내용은 여기 정리해 뒀어요 👉 {url}"
            with st.container(border=True):
                st.markdown(f"**{i}. {ptype}** · {len(text)}자")
                st.code(text, language=None, wrap_lines=True)
                if comment:
                    st.caption("첫 댓글")
                    st.code(comment, language=None, wrap_lines=True)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def render_homeplan_master(call_llm, get_trending):
    """call_llm(prompt, json_mode=False) -> str, get_trending() -> list[str]"""
    st.caption(
        "글감 찾기 → 관련 기사 선택 → 관점 추천 → 초안·제목·썸네일까지 한 번에. "
        "완성된 글은 복사해서 네이버 에디터에 직접 붙여넣으세요."
    )
    tab_home, tab_search, tab_connect, tab_threads = st.tabs(
        ["🏠 홈판 글쓰기", "🔍 검색 글쓰기", "🛍️ 쇼핑·여행 커넥트", "🧵 스레드"]
    )
    with tab_home:
        render_writing_flow("home", call_llm, get_trending)
    with tab_search:
        render_writing_flow("search", call_llm)
    with tab_connect:
        render_connect_flow(call_llm)
    with tab_threads:
        render_threads_flow(call_llm)
