import base64
import html
import json
import os
import re
from difflib import SequenceMatcher
from urllib.parse import quote_plus

import anthropic
import openai
import streamlit as st
import feedparser
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from datetime import datetime

load_dotenv()

GOOGLE_TRENDS_RSS = "https://trends.google.co.kr/trending/rss?geo=KR&hl=ko"
GOOGLE_NEWS_RSS = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"

# 카테고리별 검색 쿼리 (Google 뉴스 검색 RSS로 카테고리 전용 기사만 수집)
CATEGORY_QUERIES = {
    "경제": "경제 OR 금융 OR 증시 OR 코스피 OR 부동산 OR 기업 OR 금리",
    "정치": "정치 OR 국회 OR 대통령 OR 여야 OR 정당 OR 국정감사 OR 청와대",
    "사회": "사회 OR 사건 OR 사고 OR 법원 OR 검찰 OR 경찰 OR 교육",
    "연예계": "연예 OR 드라마 OR 영화 OR 배우 OR 가수 OR 예능 OR 아이돌",
    "건강/헬스": "건강 OR 헬스 OR 다이어트 OR 운동 OR 영양 OR 웰빙 OR 건강관리",
    "의학/병의원": "의학 OR 병원 OR 질병 OR 치료 OR 의료진 OR 신약 OR 임상시험 OR 병의원",
    "질병": "질병 OR 감염병 OR 증후군 OR 유행 OR 확진 OR 백신 OR 전염병 OR 질환",
}
CATEGORIES = ["전체"] + list(CATEGORY_QUERIES.keys())

# st.container(key=...)는 CSS 클래스명(st-key-...)으로 그대로 쓰이므로 ASCII 슬러그로 매핑
CATEGORY_SLUGS = {
    "전체": "all",
    "경제": "economy",
    "정치": "politics",
    "사회": "society",
    "연예계": "entertainment",
    "건강/헬스": "health",
    "의학/병의원": "medical",
    "질병": "disease",
}

# 제목 끝에 " - 언론사명" 형태로 붙는 출처 표기를 분리하기 위한 패턴
PUBLISHER_SUFFIX_RE = re.compile(r"\s*-\s*[^-]{1,20}$")

# 한글 자모/음절이 하나도 없는 제목(순수 영문·베트남어·한자 등 스팸성 헤드라인)을 걸러내기 위한 패턴
HANGUL_RE = re.compile(r"[가-힣ㄱ-ㆎ]")

# 광고성/무의미한 제목을 걸러내기 위한 키워드
AD_KEYWORDS = (
    "구독", "특가", "할인", "쿠폰", "이벤트", "무료체험", "광고", "협찬",
    "채용공고", "공모전", "사은품", "경품",
)

MIN_HEADLINE_LEN = 6
SIMILARITY_THRESHOLD = 0.6
TOP_N = 10
CACHE_TTL_SECONDS = 600

GEMINI_MODEL_PRIMARY = "gemini-3.6-pro"
GEMINI_MODEL_FALLBACK = "gemini-3.6-flash"

# 건강/의학/시사 등 정상적인 정보성 키워드까지 안전 필터에 걸려 응답이 비거나
# 잘리는 경우를 막기 위해, 모든 카테고리의 차단 기준을 완화한다(불법/유해 콘텐츠
# 생성을 위한 것이 아니라, 질병·증상 등을 다루는 합법적 정보 글이 과도하게
# 필터링되는 것을 막기 위함).
GEMINI_SAFETY_SETTINGS = [
    genai_types.SafetySetting(category=c, threshold="BLOCK_NONE")
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]
# gemini-3.x 계열은 답변을 쓰기 전 내부적으로 소모하는 "thinking" 토큰이
# max_output_tokens 예산을 공유한다. thinking_budget을 두지 않으면 주제에 따라
# thinking에 예산 대부분(7000+ 토큰)이 소모되어 정작 눈에 보이는 본문이
# 중간에 잘리는 문제가 있었다(실측: thoughts_token_count=7865 / 8192).
# thinking_budget으로 내부 추론 토큰을 제한하고, max_output_tokens을 충분히
# 넉넉하게 잡아 본문이 끝까지 출력되도록 한다.
GEMINI_GENERATION_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.7,
    max_output_tokens=16384,
    safety_settings=GEMINI_SAFETY_SETTINGS,
    thinking_config=genai_types.ThinkingConfig(thinking_budget=2048),
)

# ---------------------------------------------------------------------------
# AI 드림팀 설정
# 각 AI의 주특기에 맞춰 역할을 나누고, 앱(파이썬 코드)이 지휘자(오케스트레이터)가 되어
# 순서대로 일을 시키고 결과물을 취합한다.
#   - Gemini  : 리서처   — 구글 검색 연동으로 최신 팩트 브리핑 수집
#   - Claude  : 메인 작가 — 브리핑을 바탕으로 원고 집필 + 검수 의견 반영 수정
#   - ChatGPT : 검수/디자인 — 규칙 위반 검수, 이미지 프롬프트로 실제 이미지 생성
# 키가 없는 멤버는 건너뛰거나 Gemini가 대신 맡는다(아래 dream_team_write 참고).
# 모델명은 .env 로 바꿀 수 있다.
# ---------------------------------------------------------------------------
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "medium")  # low | medium | high | xhigh | max
CLAUDE_MAX_TOKENS = 32000
CLAUDE_TIMEOUT_SECONDS = 300
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.5")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
OPENAI_IMAGE_SIZE = "1536x1024"  # 16:9에 가장 가까운 가로형
OPENAI_TIMEOUT_SECONDS = 180

PROVIDERS = {
    "gemini": {"label": "Gemini", "env": "GEMINI_API_KEY"},
    "claude": {"label": "Claude", "env": "ANTHROPIC_API_KEY"},
    "openai": {"label": "ChatGPT", "env": "OPENAI_API_KEY"},
}

MODE_GEMINI = "Gemini 단독 (기존 방식)"
MODE_DREAM_TEAM = "🤝 AI 드림팀 (Gemini + Claude + ChatGPT)"

# 리서치 전용 설정: 구글 검색 그라운딩으로 최신 사실을 모은다.
GEMINI_RESEARCH_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.3,
    max_output_tokens=8192,
    safety_settings=GEMINI_SAFETY_SETTINGS,
    thinking_config=genai_types.ThinkingConfig(thinking_budget=1024),
    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
)

RESEARCH_PROMPT_TEMPLATE = """당신은 블로그 집필팀의 리서처입니다. 구글 검색을 활용해 주제 '{topic}'에 대한 최신 정보를 조사하세요.

[출력 규칙]
- 블로그 작가가 바로 참고할 수 있는 '팩트 브리핑'을 하이픈(-) 불릿으로 8~12개 작성하세요.
- 날짜, 수치, 기관명, 고유명사 등 확인된 사실 위주로 쓰고, 추측이나 의견은 쓰지 마세요.
- 확인되지 않거나 출처끼리 엇갈리는 내용은 '(미확인)'이라고 표시하세요.
- 마지막 줄에 '핵심 검색 의도:' 로 시작하는 한 줄 요약(독자가 가장 궁금해할 질문)을 덧붙이세요.
- 전체 800자 이내로 작성하세요.
"""

RESEARCH_BRIEF_INSTRUCTION = """

[리서치팀(Gemini) 최신 팩트 브리핑]
{brief}

[브리핑 활용 규칙]
- 위 브리핑은 구글 검색으로 수집한 최신 정보입니다. 본문의 사실·수치·날짜는 브리핑을 근거로 작성하세요.
- 브리핑에 없는 구체적 수치나 날짜를 지어내지 마세요. '(미확인)' 표시된 내용은 단정하지 마세요.
"""

REVIEW_SYSTEM_PROMPT = """당신은 블로그 원고 검수 전문 에디터입니다. 작가에게 전달할 수정 지시를 만드는 것이 임무입니다.
반드시 아래 JSON 형식으로만 답하세요.
{"verdict": "pass" 또는 "revise", "summary": "한 줄 총평", "issues": ["구체적인 수정 지시", ...]}
- 실제로 규칙을 어긴 부분만 issues에 넣으세요. 취향 차이나 사소한 표현은 넣지 마세요.
- 각 지시는 '어디를 어떻게 고칠지'가 드러나게 한 문장으로 쓰세요.
- 위반이 없으면 verdict를 "pass", issues를 빈 배열로 두세요."""

REVIEW_PROMPT_TEMPLATE = """[작성 규칙 원문]
{rules}

[검수 대상 원고]
{draft}

[시스템 실측값]
- 📌 본문 항목의 글자 수(공백 포함): {body_len}자 (허용 범위 1,700~1,900자)

위 원고가 작성 규칙을 지켰는지 검수하세요. 특히 다음을 확인하세요.
1. 출력 템플릿의 📌 항목 누락/순서 오류
2. 본문 분량(실측값 기준), 마크다운 표·##·구분선(---) 사용 여부
3. 검색설명 130~150자, 소제목 ✨ + 질문형, FAQ 섹션 존재
4. 과장·단정 표현(완치, 최고, 특효, 100% 등) 및 근거 없는 수치
5. 이미지 프롬프트 2세트(🎨/📝/🖼️)와 화풍 고정 문구"""

REVISION_PROMPT_TEMPLATE = """{writer_prompt}

[1차 원고]
{draft}

[검수팀(ChatGPT) 수정 지시]
{issues}

위 수정 지시를 모두 반영해 원고 전체를 다시 작성하세요. 출력 템플릿 형식을 그대로 지키고, 설명이나 머리말 없이 완성된 원고만 출력하세요."""

IMAGE_PROMPT_RE = re.compile(r"🎨\s*영문\s*프롬프트\s*[:：]\s*(.+)")
BODY_SECTION_RE = re.compile(r"📌\s*\*\*본문:?\*\*:?(.*?)(?=📌|\Z)", re.S)

# 바로한의원 공식 채널 정보 (한의원 전용 글쓰기 탭에서만 사용)
CLINIC_NAME = "바로한의원"
CLINIC_HOMEPAGE = "barohaniwon.co.kr"
CLINIC_NAVER_MAP = "https://map.naver.com/p/search/%EC%8B%A0%EC%9B%94%EB%8F%99%20%EB%B0%94%EB%A1%9C%ED%95%9C%EC%9D%98%EC%9B%90"
CLINIC_NAVER_BLOG = "https://blog.naver.com/53rmedic"
CLINIC_ADDRESS = "서울 양천구 오목로 55 신목동파라곤상가 2층 204, 205호"
CLINIC_PARKING = "건물 뒤쪽 지상주차장 이용 (2시간 무료)"
CLINIC_PHONE = "02-2602-4375"

# 생성된 글 맨 끝에 항상 그대로 덧붙이는 고정 푸터. AI가 매번 다시 쓰게 하면 전화번호/주소 같은
# 사실 정보가 미묘하게 틀어질 위험이 있어, 프롬프트로 지시하는 대신 Python 상수로 고정해
# call_gemini() 결과 뒤에 그대로 이어붙인다(아래 generate_clinic_post 참고).
CLINIC_FOOTER_TEMPLATE = f"""---
**[💡 원장님의 한마디 & 진료 안내]**

오늘 전해드린 의학 정보가 환자분들의 궁금증을 해소하는 데 작은 도움이 되셨기를 바랍니다.

인터넷상의 수많은 정보도 좋지만, 가장 중요한 것은 현재 내 몸 상태에 맞는 정확한 진단과 치료입니다. 포스팅을 읽으시면서 평소 불편하셨던 증상과 비슷한 점이 있거나, 더 깊은 상담이 필요하시다면 주저하지 말고 편하게 문의해 주세요.

양천구 신월동에서 늘 꼼꼼하고 바르게 진료하는 **{CLINIC_NAME}**이 환자분의 건강하고 편안한 일상을 위해 최선을 다해 돕겠습니다.

**🏥 {CLINIC_NAME} 진료 안내**
* **📍 오시는 길:** {CLINIC_ADDRESS}
* **🚗 주차 안내:** {CLINIC_PARKING}
* **🌐 공식 홈페이지:** http://{CLINIC_HOMEPAGE}
* **🗺️ 네이버 지도 (예약 및 리뷰):** {CLINIC_NAVER_MAP}
* **📞 진료 및 예약 문의:** {CLINIC_PHONE}
"""

BLOG_PROMPT_TEMPLATE = """
[강력 지시] 본문 전체 길이는 반드시 공백을 포함하여 1,700자 ~ 1,900자 사이로 정확히 맞춰서 작성해. 절대 2,000자를 넘기지 마.

당신은 월 천만 원의 수익을 내는 상위 1% 블로그 마케터입니다.
주어진 뉴스 헤드라인 '{headline}'을(를) 바탕으로 SEO에 완벽하게 최적화된 고품질 블로그 포스팅 원고를 작성하세요.

[블로그 글쓰기 마스터 프롬프트 규칙] (모든 글에 예외 없이 강력하게 적용)
1. 분량 제한: 본문 전체 길이는 반드시 공백 포함 1,700자 ~ 1,900자 사이로 정확히 맞추세요. 절대 2,000자를 넘기지 마세요. 겉핥기식 설명이 아닌, 원인·장단점·해결책을 깊이 있게 분석하되 분량 제한 안에서 압축적으로 작성하세요.
2. 도입부 1인칭 스토리텔링: 첫 ✨ 소제목이 나오기 전 서론에는 주제와 관련된 1인칭 화자 시점의 가상 경험담, 일상적인 고민, 또는 일화(약 300자)를 자연스럽게 포함하여 독자의 공감을 이끌어내세요. 매번 다양하고 구체적인 직업(예: 프리랜서 디자이너, 육아휴직 중인 신입 부모, 자영업자 등)을 무작위로 설정하여 현실감을 더하세요.
3. 시각적 가독성(표 사용 금지): 마크다운 표(|...|) 형식은 복사/붙여넣기 시 텍스트가 깨지므로 절대 사용하지 마세요. 개념 비교나 장단점 분석이 필요할 때는 아래 예시처럼 강조된 소제목(🔹)과 글머리 기호(-)를 활용한 구조화된 텍스트로 깔끔하게 정리하세요. 핵심 키워드나 문장은 **볼드체**로 강조하세요.

  [구조화된 텍스트 출력 예시]
  🔹 비율 분할 매도
  - 주요 타이밍: +15%, +30% 등 정해진 수익률 도달 시
  - 장점: 감정 배제, 안정적인 현금 흐름 확보
  - 단점 및 주의점: 폭등장에서 전량 보유 대비 수익률 감소

  🔹 추세 대응 매도
  - 주요 타이밍: 20일선/60일선 등 핵심 이평선 이탈 시
  - 장점: 상승 추세의 끝까지 수익 극대화 가능
  - 단점 및 주의점: 단기 변동성 및 속임수 파동에 물량 빼앗길 위험

4. 실천 가이드: 본문 후반부에는 독자가 즉시 참고할 수 있는 '요약 체크리스트'나 '행동 가이드'를 불릿 포인트(하이픈 -) 리스트로 제공하세요.
5. 전문가의 시선: 글의 마무리인 📌 나만의 시선(인사이트)에는 해당 주제나 향후 전망에 대해 통찰력 있는 분석을 덧붙여 신뢰도를 높이세요.

[필수 조건]
- 본문 내에 핵심 키워드를 3~5회 자연스럽게 반복하세요.
- {keyword_instruction}
- 주의사항: 타겟 키워드가 긴 문장이나 특수기호가 포함된 기사 헤드라인일 경우, 본문에 그 원문을 토씨 하나 틀리지 않고 기계적으로 반복 삽입하지 마세요. 핵심 명사 단어(예: 암 백신, 상용화 등) 위주로 자연스럽게 분리 및 변형하여 문맥에 부드럽게 녹여내고, 읽었을 때 로봇이 쓴 것 같은 어색한 문장이 절대 발생하지 않도록 '자연스러운 한국어 문맥'을 최우선으로 작성하세요.
- 절대 아래의 [출력 템플릿] 중 단 하나의 항목도 누락해서는 안 됩니다. 명시된 기호(📌)와 순서를 정확히 지켜서 출력하세요.
- 서론을 제외한 본문 내 소제목에는 마크다운 샵 기호(##, ###)를 절대 쓰지 마세요. 대신 모든 소제목 앞에는 '✨' 기호를 붙여서 작성하세요. (예: ✨ '암 백신' 최종테스트 통과 소식이 글로벌 바이오 시장에서 왜 큰 화제인가요?)
- 검색설명은 검색 엔진 최적화(SEO)를 위해 1~2문장으로 대충 짧게 끝내지 말고, 반드시 130자 이상 150자 이내 분량의 밀도 있는 3~4문장으로 독자를 후킹하도록 작성하세요.
- 각 항목(검색설명, 본문 등) 사이에 `--` 나 `---` 같은 하이픈/대시 구분선을 절대 임의로 삽입하지 마세요. 불필요한 공백이나 선 없이 바로 다음 📌 기호로 연결하세요.
- 이미지 프롬프트는 아래 [이미지 프롬프트 생성 규칙]을 반드시 따르세요.

[이미지 프롬프트 생성 규칙]
- 이미지 구성: 글의 흐름에 맞춰 정확히 2장의 이미지를 기획하세요.
  · 이미지 1 (도입부): 📌 검색설명과 핵심 주제를 시각화하는 이미지
  · 이미지 2 (결론부): 📌 나만의 시선(인사이트)에 담긴 미래 전망이나 해결책을 시각화하는 이미지
- 각 이미지마다 아래 3가지 항목을 세트로 묶어 깔끔하게 출력하세요.
  🎨 영문 프롬프트: (이미지 생성 AI에 그대로 복사해 넣을 수 있는 구체적인 영어 묘사)
  📝 이미지 설명: (어떤 의도로 기획된 이미지인지 한글로 짧게 설명)
  🖼️ 대체 텍스트(Alt text): (SEO 최적화를 위해 타겟 키워드를 자연스럽게 1회 포함한 한글 묘사 텍스트)
- 화풍(Style): 만화, 벡터 아트, 일러스트레이션 스타일은 절대 사용하지 마세요. 반드시 '고품질 실사 사진(Photorealistic)' 느낌으로 작성하세요.
- 내용의 연관성 및 안전한 실사화(가장 중요): 글의 핵심 주제를 직접적으로 보여주는 실사 사진으로 기획하세요. 단, 질병(매독 등)이나 혐오감을 줄 수 있는 의료 주제의 경우 환부나 징그러운 묘사 대신 "깨끗하고 전문적인 병원 진료실, 현미경을 보는 연구원, 차트를 들고 있는 의사, 깔끔한 의료 기기 매크로 샷" 등 신뢰감을 주는 전문적인 의료 환경의 실사 사진으로 대체하세요. 금융/투자 주제의 경우 "현대적인 빌딩의 유리창, 깔끔한 사무실 데스크 위 패드와 차트, 정장 차림으로 악수하는 모습" 등 현실적이고 비즈니스적인 환경으로 묘사하세요.
- 인물이 등장하는 경우, 서양인 외형이나 왜곡된 얼굴이 절대 나오지 않도록 반드시 "a photorealistic photo of an attractive and friendly modern Korean man/woman with natural, clean facial features" 같은 문구를 영문 프롬프트에 포함하세요.
- 화풍(Style) 고정: 두 이미지의 영문 프롬프트 맨 마지막에는 항상 아래 문구를 그대로 붙이세요.
  ", photorealistic, cinematic lighting, 8k resolution, shot on DSLR, highly detailed, photorealism --ar 16:9"

[GEO & AEO 최적화 규칙] (Google AI Overviews, Perplexity 등 AI 검색 엔진 노출 최적화)
- 두괄식 정답 배치(Featured Snippet 타겟): 📌 검색설명 바로 다음, 본문이 시작되는 최상단에는 독자의 검색 의도(질문)에 가장 명확하고 간결하게 답하는 1~2문장의 '핵심 요약 답변'을 가장 먼저 배치하세요. 1인칭 스토리텔링 서론은 이 핵심 요약 답변 바로 다음에 이어서 작성하세요.
- 질문형 소제목(Q&A 구조): 각 소제목(✨)은 단순 명사형이 아니라, 사용자가 검색창이나 음성으로 질문할 법한 대화형/질문형 문장으로 작성하세요. (예: "매독 전염 경로" (X) → "매독은 구체적으로 어떻게 전염될까요?" (O))
- 소제목 바로 다음 첫 문단은 서론 없이, 질문에 대한 핵심 답변을 2~3문장(50단어 내외)으로 즉시 요약해서 제공하세요 (Featured Snippet 타겟).
- 정보의 구조화(AI 파싱 최적화): 모호한 표현을 버리고 확정적이고 전문적인 어조를 사용하며, 핵심은 항상 불릿 포인트(하이픈 -)로 요약하세요. 추상적인 표현을 피하고 구체적인 수치, 명사, 팩트 중심으로 서술하여 AI 검색 엔진의 신뢰도(Information Gain)를 높이세요.
- FAQ 섹션 필수 추가: 본문 하단, 📌 나만의 시선(인사이트) 바로 위에 해당 키워드와 관련해 사람들이 가장 많이 검색하는 '자주 묻는 질문(FAQ)' 2~3가지를 선정하여 Q&A 형태로 짧고 명확하게 답변하는 섹션을 추가하세요. ChatGPT, Perplexity, 구글 AI Overviews가 답변의 출처로 인용하기 좋도록 작성하세요.

[출력 템플릿]
📌 **키워드:** (가장 검색량이 많을 만한 핵심 키워드 1개)
📌 **글제목:** (기존 헤드라인을 쓰지 말고, 키워드를 포함하여 새로 창작한 클릭 유도형 제목. 괄호·물음표·느낌표 등 특수문자 없이 자연스러운 완성형 문장으로 작성)
📌 **검색설명:** (본문 최상단에 넣을 메타 디스크립션. 130자 이상 150자 이내 분량의 밀도 있는 3~4문장으로 작성, 1~2문장으로 짧게 끝내지 말 것)
📌 **본문:**
(1~2문장 두괄식 핵심 요약 답변 → 1인칭 경험담/일화로 이어지는 서론(약 300자) → 사건의 재구성, 원인·장단점·해결책 심층 분석, 핵심 비교를 담은 🔹 구조화된 텍스트(표 금지), 요약 체크리스트/행동 가이드, 마지막으로 FAQ 2~3개(Q&A 형태) 순으로 공백 포함 1,700~1,900자(2,000자 절대 초과 금지)로 작성. 핵심 키워드/문장은 **볼드체** 강조, 소제목은 ✨로 표시, 독자의 궁금증을 해소하는 해요체 사용)
📌 **나만의 시선 (인사이트):** (이 이슈가 시장이나 사회에 미칠 파장과 향후 전망을 통찰력 있게 분석하여, 본문의 자연스러운 마무리 결론 역할을 하도록 작성. 테마주 등 심층 분석 300자)
📌 **함께 찾아보면 좋은 키워드:** (관련 검색어 3~5개)
📌 **이미지 1 (도입부):**
🎨 영문 프롬프트: (검색설명·핵심 주제를 시각화. 민감 주제는 은유적 컨셉으로 표현. 맨 끝에 화풍 고정 문구 포함)
📝 이미지 설명: (기획 의도를 한글로 짧게 설명)
🖼️ 대체 텍스트(Alt text): (타겟 키워드를 자연스럽게 1회 포함한 한글 묘사)
📌 **이미지 2 (결론부):**
🎨 영문 프롬프트: (나만의 시선에 담긴 향후 전망·해결책을 시각화. 민감 주제는 은유적 컨셉으로 표현. 맨 끝에 화풍 고정 문구 포함)
📝 이미지 설명: (기획 의도를 한글로 짧게 설명)
🖼️ 대체 텍스트(Alt text): (타겟 키워드를 자연스럽게 1회 포함한 한글 묘사)
📌 **라벨:** (글의 핵심 주제 키워드 3~5개를 쉼표(,)로 구분하여 출력. 예: 암 백신, 바이오테크, 임상결과)
📌 **퍼머링크:** (구글 검색 최적화(SEO)를 위한 영어 URL 슬러그. 핵심 주제를 띄어쓰기 없이 영문 소문자와 하이픈(-)만 사용하여 작성. 예: cancer-vaccine-final-test-success)

[강력 지시] 본문 전체 길이는 반드시 공백을 포함하여 1,700자 ~ 1,900자 사이로 정확히 맞춰서 작성해. 절대 2,000자를 넘기지 마.
"""

KEYWORD_EXPANSION_PROMPT_TEMPLATE = """사용자가 입력한 짧은 키워드 '{keyword}'를 바탕으로, 구글/네이버 검색 유입 확률이 높고 대형 블로그와 경쟁을 피할 수 있는 구체적인 질문형/해결형 '롱테일 키워드' 5개를 추천해 주세요. 각 키워드별로 검색 의도의 명확성과 경쟁도 낮음을 종합 평가한 'SEO 매력도 점수(100점 만점)'와 '추천 이유(1줄)'를 작성하세요. 출력은 반드시 마크다운 표(Table) 형식으로 작성하세요. 컬럼: | 추천 롱테일 키워드 | SEO 점수 | 추천 이유 |"""

CLINIC_PROMPT_TEMPLATE = f"""
[강력 지시] 본문 전체 길이는 반드시 공백을 포함하여 1,700자 ~ 1,900자 사이로 정확히 맞춰서 작성해. 절대 2,000자를 넘기지 마.

당신은 한의원 블로그 마케팅을 전문으로 하는 카피라이터입니다.
주어진 증상/주제 '{{topic}}'에 대해 환자들이 검색할 만한 정보를 바탕으로 SEO에 최적화된 한의원 블로그 포스팅 원고를 작성하세요.

[철저한 의료법 방어 규칙]
- 대한민국 의료법 제56조 및 관련 규정을 철저히 준수합니다.
- '완치', '최고', '특효', '부작용 0%', '100% 보장' 등 보건소, 식약처, 경찰서 등 유관기관의 단속이나 민원이 들어올 수 있는 과장되거나 단정적인 표현, 효능·효과 과대광고를 절대 금지합니다.
- 질환의 객관적인 원리, 한의학적 접근법, 일상생활 관리법 등을 차분하고 신뢰감 있는 정보 전달 목적으로만 서술하세요.
- 병원 주소·전화번호·홈페이지 등 상세 연락처는 시스템이 별도의 고정 푸터로 자동 첨부하므로, 본문에서 직접 나열하지 마세요.

[블로그 글쓰기 마스터 프롬프트 규칙] (모든 글에 예외 없이 강력하게 적용)
1. 분량 제한: 본문 전체 길이는 반드시 공백 포함 1,700자 ~ 1,900자 사이로 정확히 맞추세요. 절대 2,000자를 넘기지 마세요. 겉핥기식 설명이 아닌, 원인·장단점·해결책을 깊이 있게 분석하되 분량 제한 안에서 압축적으로 작성하세요.
2. 도입부 1인칭 스토리텔링: 첫 ✨ 소제목이 나오기 전 서론에는 증상/주제와 관련된 1인칭 화자 시점의 가상 경험담이나 일상적인 고민(약 300자)을 자연스럽게 포함하여 독자의 공감을 이끌어내세요. 매번 다양하고 구체적인 직업(예: 사무직 직장인, 프리랜서 디자이너, 자영업자 등)을 무작위로 설정하여 현실감을 더하세요. (치료 효과를 단정하는 개인 경험담이 아니라, 증상으로 인한 일상의 불편함 정도의 공감형 도입부여야 합니다.)
3. 시각적 가독성(표 사용 금지): 마크다운 표(|...|) 형식은 복사/붙여넣기 시 텍스트가 깨지므로 절대 사용하지 마세요. 개념 비교나 관리법 장단점 분석이 필요할 때는 아래 예시처럼 강조된 소제목(🔹)과 글머리 기호(-)를 활용한 구조화된 텍스트로 깔끔하게 정리하세요. 핵심 키워드나 문장은 **볼드체**로 강조하세요.

  [구조화된 텍스트 출력 예시]
  🔹 침 치료
  - 주요 대상: 급성 통증 및 근육 긴장 완화가 필요한 경우
  - 장점: 시술 즉시 통증 경감 효과, 짧은 시술 시간
  - 단점 및 주의점: 근본 원인 개선을 위해 꾸준한 내원 필요

  🔹 추나요법
  - 주요 대상: 척추·관절의 구조적 틀어짐이 의심되는 경우
  - 장점: 체형 균형 교정 및 만성 통증 완화에 효과적
  - 단점 및 주의점: 급성 염증기에는 시행 시기 조절 필요

4. 실천 가이드: 본문 후반부에는 독자가 즉시 참고할 수 있는 '요약 체크리스트'나 '일상 관리 가이드'를 불릿 포인트(하이픈 -) 리스트로 제공하세요.
5. 전문가의 시선: 관리법 체크리스트 뒤, 본문이 끝나기 직전 마지막 문단에 이 증상/주제에 대한 향후 관리 방향이나 통찰력 있는 분석을 자연스럽게 덧붙여 신뢰도를 높이세요. (단, 이는 본문의 마지막 문단일 뿐이며 별도의 마무리 인사말이나 '원장님의 한마디'가 아닙니다.)

[필수 조건]
- 본문 내에 핵심 키워드를 3~5회 자연스럽게 반복하세요.
- 주의사항: 타겟 키워드가 긴 문장이나 특수기호가 포함된 기사 헤드라인일 경우, 본문에 그 원문을 토씨 하나 틀리지 않고 기계적으로 반복 삽입하지 마세요. 핵심 명사 단어(예: 암 백신, 상용화 등) 위주로 자연스럽게 분리 및 변형하여 문맥에 부드럽게 녹여내고, 읽었을 때 로봇이 쓴 것 같은 어색한 문장이 절대 발생하지 않도록 '자연스러운 한국어 문맥'을 최우선으로 작성하세요.
- 주의: 본문 마지막 문단(전문가의 시선)까지만 작성하고, 절대 별도의 마무리 인사말이나 '원장님의 한마디'를 자체적으로 생성하지 마세요. 본문 작성이 끝나면 즉시 '📌 함께 찾아보면 좋은 키워드' 항목으로 넘어가세요.
- 절대 아래의 [출력 템플릿] 중 단 하나의 항목도 누락해서는 안 됩니다. 명시된 기호(📌)와 순서를 정확히 지켜서 출력하세요.
- 서론을 제외한 본문 내 소제목에는 마크다운 샵 기호(##, ###)를 절대 쓰지 마세요. 대신 모든 소제목 앞에는 '✨' 기호를 붙여서 작성하세요.
- 검색설명은 검색 엔진 최적화(SEO)를 위해 1~2문장으로 대충 짧게 끝내지 말고, 반드시 130자 이상 150자 이내 분량의 밀도 있는 3~4문장으로 독자를 후킹하도록 작성하세요.
- 각 항목 사이에 `--` 나 `---` 같은 하이픈/대시 구분선을 절대 임의로 삽입하지 마세요. 불필요한 공백이나 선 없이 바로 다음 📌 기호로 연결하세요.
- 이미지 프롬프트는 아래 [이미지 프롬프트 생성 규칙]을 반드시 따르세요.

[이미지 프롬프트 생성 규칙]
- 이미지 구성: 글의 흐름에 맞춰 정확히 2장의 이미지를 기획하세요.
  · 이미지 1 (도입부): 📌 검색설명과 핵심 증상/주제를 시각화하는 이미지
  · 이미지 2 (결론부): 본문 마지막 문단(전문가의 시선)에 담긴 관리 방향이나 전망을 시각화하는 이미지
- 각 이미지마다 아래 3가지 항목을 세트로 묶어 깔끔하게 출력하세요.
  🎨 영문 프롬프트: (이미지 생성 AI에 그대로 복사해 넣을 수 있는 구체적인 영어 묘사)
  📝 이미지 설명: (어떤 의도로 기획된 이미지인지 한글로 짧게 설명)
  🖼️ 대체 텍스트(Alt text): (SEO 최적화를 위해 타겟 키워드를 자연스럽게 1회 포함한 한글 묘사 텍스트)
- 화풍(Style): 만화, 벡터 아트, 일러스트레이션 스타일은 절대 사용하지 마세요. 반드시 '고품질 실사 사진(Photorealistic)' 느낌으로 작성하세요.
- 내용의 연관성 및 안전한 실사화(가장 중요): 글의 핵심 증상/주제를 직접적으로 보여주는 실사 사진으로 기획하세요. 단, 통증 부위 노출이나 시술 장면 등 혐오감을 줄 수 있는 직접 묘사 대신 "깨끗하고 전문적인 한의원 진료실, 환자에게 차트를 설명하는 한의사, 정갈하게 정돈된 침 치료 도구 매크로 샷, 바른 자세로 스트레칭하는 모습" 등 신뢰감을 주는 전문적인 의료 환경의 실사 사진으로 대체하세요.
- 인물이 등장하는 경우, 서양인 외형이나 왜곡된 얼굴이 절대 나오지 않도록 반드시 "a photorealistic photo of an attractive and friendly modern Korean man/woman with natural, clean facial features" 같은 문구를 영문 프롬프트에 포함하세요.
- 화풍(Style) 고정: 두 이미지의 영문 프롬프트 맨 마지막에는 항상 아래 문구를 그대로 붙이세요.
  ", photorealistic, cinematic lighting, 8k resolution, shot on DSLR, highly detailed, photorealism --ar 16:9"

[GEO & AEO 최적화 규칙] (Google AI Overviews, Perplexity 등 AI 검색 엔진 노출 최적화)
- 두괄식 정답 배치(Featured Snippet 타겟): 📌 검색설명 바로 다음, 본문이 시작되는 최상단에는 환자의 검색 의도(질문)에 가장 명확하고 간결하게 답하는 1~2문장의 '핵심 요약 답변'을 가장 먼저 배치하세요. 1인칭 공감형 서론은 이 핵심 요약 답변 바로 다음에 이어서 작성하세요.
- 질문형 소제목(Q&A 구조): 각 소제목(✨)은 단순 명사형이 아니라, 환자가 검색창이나 음성으로 질문할 법한 대화형/질문형 문장으로 작성하세요. (예: "허리 디스크 원인" (X) → "허리 디스크는 왜 생기는 걸까요?" (O))
- 소제목 바로 다음 첫 문단은 서론 없이, 질문에 대한 핵심 답변을 2~3문장(50단어 내외)으로 즉시 요약해서 제공하세요 (Featured Snippet 타겟).
- 정보의 구조화(AI 파싱 최적화): 모호한 표현을 버리고 확정적이고 전문적인 어조를 사용하며, 핵심은 항상 불릿 포인트(하이픈 -)로 요약하세요. 추상적인 표현을 피하고 구체적인 수치, 명사, 팩트 중심으로 서술하여 AI 검색 엔진의 신뢰도(Information Gain)를 높이세요.
- FAQ 섹션 필수 추가: 생활 관리 체크리스트 뒤, 마지막 문단(전문가의 시선) 바로 위에 해당 증상/주제와 관련해 환자들이 가장 많이 검색하는 '자주 묻는 질문(FAQ)' 2~3가지를 선정하여 Q&A 형태로 짧고 명확하게 답변하는 섹션을 추가하세요.

[출력 템플릿]
📌 **키워드:** (가장 검색량이 많을 만한 핵심 키워드 1개)
📌 **글제목:** (증상/주제를 반영해 새로 창작한 클릭 유도형 제목. 괄호·물음표·느낌표 등 특수문자 없이 자연스러운 완성형 문장으로 작성)
📌 **검색설명:** (본문 최상단에 넣을 메타 디스크립션. 130자 이상 150자 이내 분량의 밀도 있는 3~4문장으로 작성, 1~2문장으로 짧게 끝내지 말 것)
📌 **본문:**
(1~2문장 두괄식 핵심 요약 답변 → 1인칭 공감형 도입부(약 300자) → 원인, 증상 설명, 한의학적 접근, 핵심 비교를 담은 🔹 구조화된 텍스트(표 금지), 생활 관리 체크리스트, FAQ 2~3개(Q&A 형태), 마지막 문단의 전문가 시선(향후 전망) 순으로 공백 포함 1,700~1,900자(2,000자 절대 초과 금지)로 작성. 핵심 키워드/문장은 **볼드체** 강조, 소제목은 ✨로 표시, 독자의 궁금증을 해소하는 해요체 사용. 마무리 인사말이나 원장님의 한마디는 별도로 쓰지 않고 전문가 시선 문단에서 바로 끝낼 것)
📌 **함께 찾아보면 좋은 키워드:** (관련 검색어 3~5개)
📌 **이미지 1 (도입부):**
🎨 영문 프롬프트: (검색설명·핵심 증상을 시각화. 민감 주제는 은유적 컨셉으로 표현. 맨 끝에 화풍 고정 문구 포함)
📝 이미지 설명: (기획 의도를 한글로 짧게 설명)
🖼️ 대체 텍스트(Alt text): (타겟 키워드를 자연스럽게 1회 포함한 한글 묘사)
📌 **이미지 2 (결론부):**
🎨 영문 프롬프트: (본문 마지막 문단의 관리 방향·전망을 시각화. 민감 주제는 은유적 컨셉으로 표현. 맨 끝에 화풍 고정 문구 포함)
📝 이미지 설명: (기획 의도를 한글로 짧게 설명)
🖼️ 대체 텍스트(Alt text): (타겟 키워드를 자연스럽게 1회 포함한 한글 묘사)
📌 **라벨:** (글의 핵심 주제 키워드 3~5개를 쉼표(,)로 구분하여 출력. 예: 암 백신, 바이오테크, 임상결과)
📌 **퍼머링크:** (구글 검색 최적화(SEO)를 위한 영어 URL 슬러그. 핵심 주제를 띄어쓰기 없이 영문 소문자와 하이픈(-)만 사용하여 작성. 예: cancer-vaccine-final-test-success)

[강력 지시] 본문 전체 길이는 반드시 공백을 포함하여 1,700자 ~ 1,900자 사이로 정확히 맞춰서 작성해. 절대 2,000자를 넘기지 마.
"""


def get_api_key(provider):
    # 사이드바에서 직접 입력/초기화한 키를 항상 최우선으로 사용한다.
    session_key = st.session_state.get(f"{provider}_api_key", "")
    if session_key:
        return session_key

    # '초기화' 버튼을 누른 직후에는 .env/secrets 값이 남아있어도 무시하고
    # 빈 입력창을 보여줘야 하므로, 여기서 즉시 빈 값을 반환한다.
    if st.session_state.get(f"ignore_env_key_{provider}"):
        return ""

    env_name = PROVIDERS[provider]["env"]
    env_key = os.getenv(env_name)
    if env_key:
        return env_key

    try:
        secrets_key = st.secrets.get(env_name)
    except Exception:
        secrets_key = None
    if secrets_key:
        return secrets_key

    return ""


def get_gemini_api_key():
    return get_api_key("gemini")


GEMINI_TIMEOUT_MS = 60_000  # 네트워크가 응답 없이 무한정 대기하지 않도록 60초 타임아웃 강제


@st.cache_resource(show_spinner=False)
def get_gemini_client(api_key):
    return genai.Client(
        api_key=api_key,
        http_options=genai.types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )


def call_gemini(prompt, config=GEMINI_GENERATION_CONFIG):
    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Gemini API 키가 설정되지 않았습니다. 사이드바에서 입력해주세요.")

    client = get_gemini_client(api_key)

    try:
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL_PRIMARY, contents=prompt, config=config
            )
        except genai_errors.APIError:
            # gemini-3.6-pro를 사용할 수 없는 계정/리전인 경우 gemini-3.6-flash로 폴백
            response = client.models.generate_content(
                model=GEMINI_MODEL_FALLBACK, contents=prompt, config=config
            )
    except Exception as e:
        # API 키 오류, 네트워크 타임아웃, 할당량 초과 등 폴백 호출까지 실패한 모든 경우를
        # 여기서 한 번에 잡아 스피너가 무한 로딩에 빠지지 않고 즉시 중단되도록 한다.
        raise RuntimeError(
            "API 호출 중 문제가 발생했습니다. API 키나 네트워크 상태를 확인해 주세요. "
            f"(에러 내용: {e})"
        ) from e

    text = getattr(response, "text", None)
    if not text:
        # 세이프티 필터 등으로 후보 텍스트가 비어 오는 경우 원인을 그대로 노출
        finish_reason = None
        if getattr(response, "candidates", None):
            finish_reason = getattr(response.candidates[0], "finish_reason", None)
        raise RuntimeError(f"Gemini 응답에 본문이 없습니다 (finish_reason={finish_reason})")

    return text


@st.cache_resource(show_spinner=False)
def get_claude_client(api_key):
    return anthropic.Anthropic(api_key=api_key, timeout=CLAUDE_TIMEOUT_SECONDS)


@st.cache_resource(show_spinner=False)
def get_openai_client(api_key):
    return openai.OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)


def call_claude(prompt, system=None):
    api_key = get_api_key("claude")
    if not api_key:
        raise RuntimeError("Claude API 키가 설정되지 않았습니다. 사이드바에서 입력해주세요.")

    client = get_claude_client(api_key)
    request = dict(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        output_config={"effort": CLAUDE_EFFORT},
        messages=[{"role": "user", "content": prompt}],
    )
    if system:
        request["system"] = system
    if CLAUDE_MODEL == "claude-opus-5-5":
        # 안전 분류기가 요청을 거절(refusal)하면 서버가 자동으로 대체 모델로 이어서 답하게 한다.
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["fallbacks"] = "default"

    try:
        # 긴 원고는 응답 시간이 길어 HTTP 타임아웃을 피하려고 스트리밍으로 받는다.
        with client.beta.messages.stream(**request) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Claude API 키가 올바르지 않습니다. 사이드바에서 다시 입력해 주세요.") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Claude 사용량 한도에 걸렸습니다. 잠시 후 다시 시도해 주세요.") from e
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude API 호출 중 문제가 발생했습니다. (에러 내용: {e})") from e

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude가 이 주제의 작성을 거절했습니다. 주제를 바꾸거나 Gemini 단독 모드로 시도해 주세요.")

    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise RuntimeError(f"Claude 응답에 본문이 없습니다 (stop_reason={message.stop_reason})")
    return text


def call_openai(prompt, system=None, json_mode=False):
    api_key = get_api_key("openai")
    if not api_key:
        raise RuntimeError("ChatGPT(OpenAI) API 키가 설정되지 않았습니다. 사이드바에서 입력해주세요.")

    client = get_openai_client(api_key)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {"model": OPENAI_TEXT_MODEL, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**kwargs)
    except openai.AuthenticationError as e:
        raise RuntimeError("ChatGPT API 키가 올바르지 않습니다. 사이드바에서 다시 입력해 주세요.") from e
    except openai.OpenAIError as e:
        raise RuntimeError(f"ChatGPT API 호출 중 문제가 발생했습니다. (에러 내용: {e})") from e

    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("ChatGPT 응답에 본문이 없습니다.")
    return text


def generate_images_with_openai(prompts):
    """원고의 🎨 영문 프롬프트로 실제 이미지를 생성해 PNG 바이트 리스트로 돌려준다."""
    api_key = get_api_key("openai")
    if not api_key:
        raise RuntimeError("이미지 생성에는 ChatGPT(OpenAI) API 키가 필요합니다.")

    client = get_openai_client(api_key)
    images = []
    for prompt in prompts:
        # '--ar 16:9' 같은 미드저니 전용 파라미터는 OpenAI가 이해하지 못하므로 떼고 size로 지정한다.
        clean_prompt = re.sub(r"\s--\w+\s+\S+", "", prompt).strip()
        try:
            result = client.images.generate(
                model=OPENAI_IMAGE_MODEL, prompt=clean_prompt, size=OPENAI_IMAGE_SIZE, n=1
            )
        except openai.OpenAIError as e:
            raise RuntimeError(f"이미지 생성 중 문제가 발생했습니다. (에러 내용: {e})") from e
        images.append(base64.b64decode(result.data[0].b64_json))
    return images


def extract_image_prompts(draft):
    return [m.strip() for m in IMAGE_PROMPT_RE.findall(draft or "") if m.strip()]


def measure_body_length(draft):
    match = BODY_SECTION_RE.search(draft or "")
    return len(match.group(1).strip()) if match else len((draft or "").strip())


def is_dream_team_mode():
    return st.session_state.get("writing_mode") == MODE_DREAM_TEAM


def research_with_gemini(topic):
    return call_gemini(RESEARCH_PROMPT_TEMPLATE.format(topic=topic), config=GEMINI_RESEARCH_CONFIG)


def review_with_openai(rules, draft):
    raw = call_openai(
        REVIEW_PROMPT_TEMPLATE.format(rules=rules, draft=draft, body_len=measure_body_length(draft)),
        system=REVIEW_SYSTEM_PROMPT,
        json_mode=True,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"verdict": "pass", "summary": "검수 결과를 해석하지 못해 1차 원고를 그대로 사용합니다.", "issues": []}
    issues = [str(i).strip() for i in data.get("issues") or [] if str(i).strip()]
    verdict = "revise" if data.get("verdict") == "revise" and issues else "pass"
    return {"verdict": verdict, "summary": str(data.get("summary", "")).strip(), "issues": issues}


def dream_team_write(writer_prompt, topic, progress=None):
    """앱이 지휘자가 되어 Gemini(리서치) → Claude(집필) → ChatGPT(검수) → Claude(수정) 순으로 일을 시킨다.

    키가 없는 멤버는 건너뛰거나 Gemini가 대신 맡는다. (원고, 작업 리포트 마크다운)을 돌려준다.
    """
    report = progress or (lambda _msg: None)
    log = []

    def note(msg):
        log.append(msg)
        report(msg)

    has_gemini = bool(get_api_key("gemini"))
    has_claude = bool(get_api_key("claude"))
    has_openai = bool(get_api_key("openai"))
    if not (has_gemini or has_claude):
        raise RuntimeError("원고를 쓸 작가가 없습니다. Claude 또는 Gemini API 키를 입력해 주세요.")

    # 1) 리서치: Gemini + 구글 검색
    prompt = writer_prompt
    if has_gemini:
        note("🔎 **Gemini(리서처)**: 구글 검색으로 최신 팩트를 수집하는 중...")
        try:
            brief = research_with_gemini(topic)
            prompt = writer_prompt + RESEARCH_BRIEF_INSTRUCTION.format(brief=brief)
            note(f"🔎 **Gemini(리서처)** 팩트 브리핑 완료\n\n{brief}")
        except Exception as e:  # 리서치는 보조 단계라 실패해도 집필은 계속한다.
            note(f"⚠️ 리서치 단계를 건너뜁니다: {e}")
    else:
        note("⏭️ Gemini 키가 없어 리서치 단계를 건너뜁니다.")

    # 2) 집필: Claude (없으면 Gemini가 대신)
    writer_name = "Claude" if has_claude else "Gemini"
    writer = call_claude if has_claude else call_gemini
    note(f"✍️ **{writer_name}(메인 작가)**: 원고를 집필하는 중...")
    draft = writer(prompt)
    note(f"✍️ **{writer_name}(메인 작가)** 1차 원고 완료 (본문 {measure_body_length(draft):,}자)")

    # 3) 검수: ChatGPT → 지적사항이 있으면 작가가 한 번 수정
    if has_openai:
        note("🧐 **ChatGPT(검수자)**: 작성 규칙 위반 여부를 검수하는 중...")
        try:
            review = review_with_openai(writer_prompt, draft)
        except Exception as e:
            review = None
            note(f"⚠️ 검수 단계를 건너뜁니다: {e}")
        if review:
            issues_md = "\n".join(f"- {issue}" for issue in review["issues"])
            note(f"🧐 **ChatGPT(검수자)** 총평: {review['summary'] or '-'}" + (f"\n\n{issues_md}" if issues_md else ""))
            if review["verdict"] == "revise":
                note(f"🔁 **{writer_name}(메인 작가)**: 검수 의견을 반영해 수정하는 중...")
                draft = writer(
                    REVISION_PROMPT_TEMPLATE.format(writer_prompt=prompt, draft=draft, issues=issues_md)
                )
                note(f"🔁 **{writer_name}(메인 작가)** 수정본 완료 (본문 {measure_body_length(draft):,}자)")
            else:
                note("✅ 검수 통과 — 1차 원고를 최종본으로 확정합니다.")
    else:
        note("⏭️ ChatGPT 키가 없어 검수 단계를 건너뜁니다.")

    return draft, "\n\n".join(log)


def write_post(prompt, topic, progress=None):
    """현재 작성 모드에 맞춰 원고를 만든다. (원고, 드림팀 리포트 또는 None)을 돌려준다."""
    if is_dream_team_mode():
        return dream_team_write(prompt, topic, progress)
    return call_gemini(prompt), None


def build_blog_prompt(headline, target_keyword=""):
    target_keyword = (target_keyword or "").strip()
    if target_keyword:
        keyword_instruction = (
            f"반드시 사용자가 지정한 키워드 '{target_keyword}'을(를) 메인 타겟 키워드로 사용하여 "
            "글을 작성하세요. 임의로 다른 키워드를 추출하지 마세요."
        )
    else:
        keyword_instruction = "뉴스 헤드라인을 분석해 롱테일 키워드를 1개 자동 추출하세요."

    return BLOG_PROMPT_TEMPLATE.format(headline=headline, keyword_instruction=keyword_instruction)


def generate_blog_post(headline, target_keyword="", progress=None):
    return write_post(build_blog_prompt(headline, target_keyword), headline, progress)


def expand_keyword(keyword):
    prompt = KEYWORD_EXPANSION_PROMPT_TEMPLATE.format(keyword=keyword)
    return call_gemini(prompt)


def generate_clinic_post(topic, progress=None):
    prompt = CLINIC_PROMPT_TEMPLATE.format(topic=topic)
    text, team_log = write_post(prompt, topic, progress)
    # 주소/전화번호 등은 AI가 매번 다시 쓰게 하지 않고, 고정 푸터를 그대로 이어붙인다.
    return f"{text}\n\n{CLINIC_FOOTER_TEMPLATE}", team_log


TAG_RE = re.compile(r"<[^>]+>")


def clean_html(text):
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def strip_publisher_suffix(title):
    result = title.strip()
    # 언론사 접미사가 중복으로 붙는 경우("... - 머니투데이 - 머니투데이")까지 반복 제거
    for _ in range(3):
        stripped = PUBLISHER_SUFFIX_RE.sub("", result).strip()
        # 더 뗄 게 없거나, 떼어냈을 때 헤드라인이 너무 짧아지면(언론사명뿐인 제목) 중단
        if stripped == result or len(stripped) < MIN_HEADLINE_LEN:
            break
        result = stripped
    return result


def is_promotional(title):
    return any(keyword in title for keyword in AD_KEYWORDS)


def has_hangul(text):
    return bool(HANGUL_RE.search(text))


def category_feed_urls(category):
    if category == "전체":
        return [GOOGLE_TRENDS_RSS, GOOGLE_NEWS_RSS]
    query = CATEGORY_QUERIES[category]
    return [GOOGLE_NEWS_SEARCH_URL.format(query=quote_plus(query))]


def fetch_headlines(urls):
    headlines = []
    for url in urls:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            raw_title = clean_html(entry.get("title", ""))
            if not raw_title:
                continue

            headline = strip_publisher_suffix(raw_title)
            if len(headline) < MIN_HEADLINE_LEN or is_promotional(headline):
                continue
            if not has_hangul(headline):
                # 한글이 한 글자도 없는 제목(순수 영문/베트남어/한자 등 스팸성 키워드)은 제외
                continue

            headlines.append(headline)
    return headlines


def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


def rank_headlines(headlines, top_n=TOP_N):
    clusters = []  # [{"text": 대표 헤드라인, "count": 언급 수, "order": 최초 등장 순서(=최신순)}]

    for order, headline in enumerate(headlines):
        matched = None
        for cluster in clusters:
            if similar(headline, cluster["text"]) >= SIMILARITY_THRESHOLD:
                matched = cluster
                break

        if matched:
            matched["count"] += 1
            # 더 긴(정보량이 많은) 제목을 대표 헤드라인으로 채택
            if len(headline) > len(matched["text"]):
                matched["text"] = headline
        else:
            clusters.append({"text": headline, "count": 1, "order": order})

    # 최신 수집분을 우선으로: 등장 순서(=피드 상 최신순) 기준 정렬
    clusters.sort(key=lambda c: c["order"])
    return [(c["text"], c["count"]) for c in clusters[:top_n]]


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def get_news_data(category):
    urls = category_feed_urls(category)
    raw_headlines = fetch_headlines(urls)
    headlines = rank_headlines(raw_headlines, top_n=TOP_N)
    return {
        "headlines": headlines,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def inject_compact_style():
    # 아래쪽 뉴스 리스트 컨테이너(st.container(key=f"headline_rows_{tab_key}"),
    # 클래스는 .st-key-headline_rows_ 로 시작)에만 적용되도록 범위를 한정한다.
    # 메인 타이틀(h1) 등 다른 영역은 Streamlit 기본 여백/line-height를 그대로 사용한다.
    st.html(
        """
        <style>
        /* 헤드라인 리스트 내부 요소 간 세로 간격 축소 */
        div[class*="st-key-headline_rows_"] div[data-testid="stVerticalBlock"] {
            gap: 0.15rem;
        }
        /* 헤드라인 텍스트/버튼이 들어가는 컬럼의 상하 패딩 제거 */
        div[class*="st-key-headline_rows_"] div[data-testid="column"] {
            padding-top: 0rem;
            padding-bottom: 0rem;
        }
        div[class*="st-key-headline_rows_"] div[data-testid="stMarkdownContainer"] p {
            margin: 0;
            padding: 0.1rem 0;
            font-size: 0.85rem;
            line-height: 1.3;
        }
        /* 복사하기 / 블로그 글쓰기 버튼 크기 축소 */
        div[class*="st-key-headline_rows_"] div[data-testid="stButton"] > button {
            padding: 0.1rem 0.6rem;
            font-size: 0.75rem;
            min-height: 1.6rem;
            height: 1.6rem;
            line-height: 1;
        }
        .headline-sep {
            border: none;
            border-top: 1px solid rgba(128, 128, 128, 0.25);
            margin: 0.1rem 0;
        }
        </style>
        """
    )


def clear_keyword(key_name):
    st.session_state[key_name] = ""


def set_keyword_value(key_name, value):
    st.session_state[key_name] = value.strip()


def select_and_trigger_write(keyword_key, value, trigger_key):
    # 콜백에서는 상태만 확정하고, 실제 Gemini 호출(+ 스피너)은 콜백 밖의
    # 일반 스크립트 흐름에서 이 트리거 플래그를 보고 수행한다.
    # (콜백 안에서 st.spinner 등 UI 요소를 직접 그리는 것은 권장되지 않는다.)
    st.session_state[keyword_key] = value.strip()
    st.session_state[trigger_key] = True


def reset_api_key(provider="gemini"):
    st.session_state[f"{provider}_api_key"] = ""
    st.session_state[f"ignore_env_key_{provider}"] = True
    if f"{provider}_api_key_input" in st.session_state:
        del st.session_state[f"{provider}_api_key_input"]


def apply_api_key(provider="gemini"):
    # 위젯 자신의 key({provider}_api_key_input)에서 값을 읽어와, 별도의
    # 세션 변수({provider}_api_key)에 확정적으로 덮어쓴다. text_input의 반환값을
    # 매 rerun마다 암묵적으로 미러링하던 기존 방식은 붙여넣기 타이밍에 따라
    # 값이 반영되지 않을 수 있어, 명시적인 "적용" 버튼 클릭 시점에만 확정한다.
    # (on_click 콜백이 끝나면 Streamlit이 자동으로 rerun 하므로 st.rerun()은 필요 없다.)
    st.session_state[f"{provider}_api_key"] = st.session_state.get(f"{provider}_api_key_input", "").strip()
    st.session_state[f"ignore_env_key_{provider}"] = False


def clear_draft(draft_key):
    # 초안과 함께 드림팀 리포트/생성 이미지도 같이 지운다.
    st.session_state[draft_key] = ""
    st.session_state.pop(f"{draft_key}_team_log", None)
    st.session_state.pop(f"{draft_key}_images", None)


def run_draft_generation(draft_key, generate, spinner_text):
    """generate(progress) -> (원고, 드림팀 리포트)를 실행하고 결과를 세션에 저장한다.

    드림팀 모드에서는 각 AI 멤버의 진행 상황을 st.status 안에 실시간으로 보여준다.
    """
    try:
        if is_dream_team_mode():
            with st.status(f"🤝 AI 드림팀 가동 중 — {spinner_text}", expanded=True) as status:
                try:
                    text, team_log = generate(status.markdown)
                except Exception:
                    status.update(label="❌ 드림팀 작업 실패", state="error", expanded=True)
                    raise
                status.update(label="✅ 드림팀 작업 완료", state="complete", expanded=False)
        else:
            with st.spinner(spinner_text):
                text, team_log = generate(None)
    except Exception as e:
        st.error(str(e))
        return

    st.session_state[draft_key] = text
    st.session_state.pop(f"{draft_key}_images", None)
    if team_log:
        st.session_state[f"{draft_key}_team_log"] = team_log
    else:
        st.session_state.pop(f"{draft_key}_team_log", None)


def render_team_outputs(draft_key):
    team_log = st.session_state.get(f"{draft_key}_team_log")
    if team_log:
        with st.expander("🤝 AI 드림팀 작업 리포트", expanded=False):
            st.markdown(team_log)

    prompts = extract_image_prompts(st.session_state.get(draft_key, ""))
    if not prompts:
        return

    images_key = f"{draft_key}_images"
    has_openai = bool(get_api_key("openai"))
    if st.button(
        f"🎨 ChatGPT로 이미지 {len(prompts)}장 생성",
        key=f"gen_images_{draft_key}",
        disabled=not has_openai,
        help=None if has_openai else "사이드바에 ChatGPT(OpenAI) API 키를 입력하면 사용할 수 있습니다.",
    ):
        try:
            with st.spinner("ChatGPT가 이미지를 그리고 있습니다..."):
                st.session_state[images_key] = generate_images_with_openai(prompts)
        except Exception as e:
            st.error(str(e))

    images = st.session_state.get(images_key) or []
    if images:
        cols = st.columns(len(images))
        labels = ["이미지 1 (도입부)", "이미지 2 (결론부)"]
        for i, (col, image) in enumerate(zip(cols, images)):
            label = labels[i] if i < len(labels) else f"이미지 {i + 1}"
            with col:
                st.image(image, caption=label, width="stretch")
                st.download_button(
                    f"{label} 다운로드",
                    data=image,
                    file_name=f"{draft_key}_image_{i + 1}.png",
                    mime="image/png",
                    key=f"dl_{draft_key}_{i}",
                )


TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")


def parse_markdown_table(text):
    """마크다운 표를 (표 앞 설명글, 헤더, 데이터 행 리스트)로 분리한다."""
    prose_lines = []
    header = None
    rows = []
    in_table = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            in_table = True
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if header is None:
                header = cells
            elif all(TABLE_SEP_CELL_RE.match(c) for c in cells):
                continue  # 구분선(:---:) 행은 건너뜀
            else:
                rows.append(cells)
        elif not in_table:
            prose_lines.append(line)

    return "\n".join(prose_lines).strip(), header or [], rows


def render_keyword_suggestions(expansion_text, keyword_key, id_prefix):
    prose, header, rows = parse_markdown_table(expansion_text)

    with st.container(border=True):
        if prose:
            st.markdown(prose)

        if not rows:
            # 표 파싱에 실패한 경우 원문 그대로 표시(안전장치)
            st.markdown(expansion_text)
            return

        labels = (header[:3] if len(header) >= 3 else ["추천 롱테일 키워드", "SEO 점수", "추천 이유"]) + ["글쓰기"]
        header_cols = st.columns([3, 1, 3, 1], gap="small")
        for col, label in zip(header_cols, labels):
            col.markdown(f"**{label}**")

        trigger_key = f"auto_write_{id_prefix}"
        for i, row in enumerate(rows):
            keyword_text = row[0] if len(row) > 0 else ""
            score_text = row[1] if len(row) > 1 else ""
            reason_text = row[2] if len(row) > 2 else ""

            cols = st.columns([3, 1, 3, 1], gap="small")
            cols[0].markdown(keyword_text)
            cols[1].markdown(score_text)
            cols[2].markdown(reason_text)
            with cols[3]:
                st.button(
                    "글쓰기",
                    key=f"select_kw_{id_prefix}_{i}",
                    on_click=select_and_trigger_write,
                    args=(keyword_key, re.sub(r"\*\*", "", keyword_text), trigger_key),
                )


def render_keyword_and_draft_panel(
    id_prefix,
    keyword_key,
    expansion_key,
    draft_key,
    generate_fn,
    write_button_label,
    spinner_text,
    draft_heading,
):
    st.markdown("🎯 타겟 키워드 (직접 입력하거나 비워두면 AI가 자동 추출합니다)")
    # text_input은 Enter나 포커스 이탈 전에는 값이 서버로 커밋되지 않아, 입력 직후 바로
    # 옆 버튼을 클릭하면 "직전" 값으로 처리될 수 있다(둘 다 세션 상태를 직접 참조해도
    # 발생하는 프런트엔드 타이밍 문제). st.form으로 묶으면 제출 버튼 클릭 시 폼 안의 모든
    # 위젯 값이 함께 커밋되는 것이 보장되므로, 화면에 보이는 최신 입력값이 100% 반영된다.
    with st.form(key=f"keyword_form_{id_prefix}", border=False):
        col_input, col_expand, col_write, col_clear = st.columns(
            [5.1, 1.3, 1.6, 1.1], gap="small"
        )
        with col_input:
            st.text_input(
                "🎯 타겟 키워드",
                key=keyword_key,
                label_visibility="collapsed",
            )
        with col_expand:
            expand_clicked = st.form_submit_button("✨ AI 확장")
        with col_write:
            write_clicked = st.form_submit_button(write_button_label)
        with col_clear:
            # text_input이 이미 이 run에서 인스턴스화된 뒤이므로, 스크립트 본문에서 직접
            # session_state[keyword_key]를 수정하면 StreamlitAPIException이 발생한다.
            # on_click 콜백은 위젯 인스턴스화보다 먼저 실행되므로 여기서만 안전하다.
            st.form_submit_button(
                "지우기",
                on_click=clear_keyword,
                args=(keyword_key,),
            )

    if expand_clicked:
        current_keyword = st.session_state.get(keyword_key, "").strip()
        if not current_keyword:
            st.warning("먼저 확장할 키워드를 입력해주세요.")
        else:
            try:
                with st.spinner("황금 롱테일 키워드 발굴 중..."):
                    st.session_state[expansion_key] = expand_keyword(current_keyword)
            except Exception as e:
                st.error(str(e))

    if write_clicked:
        current_keyword = st.session_state.get(keyword_key, "").strip()
        if not current_keyword:
            st.warning("먼저 타겟 키워드를 입력해주세요.")
        else:
            run_draft_generation(
                draft_key, lambda progress: generate_fn(current_keyword, progress), spinner_text
            )

    if st.session_state[expansion_key]:
        render_keyword_suggestions(st.session_state[expansion_key], keyword_key, id_prefix)

    # 추천 키워드 표의 "글쓰기" 버튼이 눌리면(select_and_trigger_write 콜백이 세운 플래그),
    # 사용자가 '키워드로 글쓰기'를 따로 누르지 않아도 여기서 바로 이어서 생성한다.
    trigger_key = f"auto_write_{id_prefix}"
    if st.session_state.get(trigger_key):
        st.session_state[trigger_key] = False
        current_keyword = st.session_state.get(keyword_key, "").strip()
        if current_keyword:
            run_draft_generation(
                draft_key, lambda progress: generate_fn(current_keyword, progress), spinner_text
            )

    col1, col2 = st.columns([8.5, 1.5], gap="small")
    with col1:
        st.subheader(draft_heading)
    with col2:
        st.button(
            "초안 지우기",
            key=f"clear_draft_{id_prefix}",
            on_click=clear_draft,
            args=(draft_key,),
        )

    st.info("💡 출력된 결과물 우측 상단의 '📋 복사 아이콘'을 누르면 한 번에 전체 복사됩니다.")
    st.code(
        st.session_state.get(draft_key, ""),
        language="markdown",
        height=800,
        wrap_lines=True,
    )
    render_team_outputs(draft_key)


def render_headline_list(headlines, tab_key):
    # 초안은 st.code로 표시(읽기 전용)하므로 위젯 자체 상태와의 동기화 문제가 없다.
    draft_key = f"blog_draft_{tab_key}"
    if draft_key not in st.session_state:
        st.session_state[draft_key] = ""

    keyword_key = f"target_keyword_{tab_key}"
    if keyword_key not in st.session_state:
        st.session_state[keyword_key] = ""

    expansion_key = f"keyword_expansion_{tab_key}"
    if expansion_key not in st.session_state:
        st.session_state[expansion_key] = ""

    if not headlines:
        st.info("해당 카테고리의 헤드라인이 없습니다.")
        return

    slug = CATEGORY_SLUGS.get(tab_key, tab_key)
    with st.container(key=f"headline_rows_{slug}"):
        for rank, (headline, _) in enumerate(headlines, start=1):
            col_text, col_kw, col_blog = st.columns([4.6, 1.9, 1.6], gap="small")
            with col_text:
                # st.code 자체에 내장된 복사 아이콘으로 한 번에 복사할 수 있도록,
                # 헤드라인 표시와 복사 기능을 하나로 합쳤다(외부 클립보드 라이브러리 불필요).
                st.code(f"{rank}. {headline}", language=None, height=70, wrap_lines=True)
            with col_kw:
                st.button(
                    "타겟키워드로 복사",
                    key=f"copy_kw_row_{tab_key}_{rank}",
                    on_click=set_keyword_value,
                    args=(keyword_key, headline),
                )
            with col_blog:
                # on_click 콜백이 스크립트 본문 실행 전에 먼저 타겟 키워드 칸을 이 헤드라인으로
                # 채워주므로, 아래 if 블록에서 session_state를 읽을 때는 이미 갱신된 값이 보인다.
                if st.button(
                    "블로그 글쓰기(1800자+)",
                    key=f"blog_{tab_key}_{rank}",
                    on_click=set_keyword_value,
                    args=(keyword_key, headline),
                ):
                    target_keyword = st.session_state.get(keyword_key, "")
                    run_draft_generation(
                        draft_key,
                        lambda progress, h=headline, kw=target_keyword: generate_blog_post(
                            h, target_keyword=kw, progress=progress
                        ),
                        "AI가 블로그 초안을 작성하고 있습니다...",
                    )
            st.html('<hr class="headline-sep">')

    render_keyword_and_draft_panel(
        id_prefix=tab_key,
        keyword_key=keyword_key,
        expansion_key=expansion_key,
        draft_key=draft_key,
        generate_fn=lambda kw, progress: generate_blog_post(kw, target_keyword=kw, progress=progress),
        write_button_label="키워드로 글쓰기",
        spinner_text="AI가 블로그 초안을 작성하고 있습니다...",
        draft_heading="블로그 초안",
    )


def render_clinic_tab():
    keyword_key = "clinic_topic"
    if keyword_key not in st.session_state:
        st.session_state[keyword_key] = ""

    expansion_key = "clinic_keyword_expansion"
    if expansion_key not in st.session_state:
        st.session_state[expansion_key] = ""

    draft_key = "clinic_draft"
    if draft_key not in st.session_state:
        st.session_state[draft_key] = ""

    st.caption(
        f"공식 홈페이지: [{CLINIC_HOMEPAGE}](https://{CLINIC_HOMEPAGE}) · "
        f"[네이버 지도에서 찾아오는 길]({CLINIC_NAVER_MAP}) · "
        f"[네이버 블로그]({CLINIC_NAVER_BLOG})"
    )

    render_keyword_and_draft_panel(
        id_prefix="clinic",
        keyword_key=keyword_key,
        expansion_key=expansion_key,
        draft_key=draft_key,
        generate_fn=generate_clinic_post,
        write_button_label="한의원 키워드로 글쓰기",
        spinner_text="AI가 한의원 블로그 초안을 작성하고 있습니다...",
        draft_heading="한의원 블로그 초안",
    )


GUIDELINE_TEXT = """
**[공통 요건]**
- 본문 분량: 공백 포함 **1,700~1,900자** (2,000자 절대 초과 금지)
- 소제목 기호: 마크다운 `##`/`###` 대신 **✨** 사용
- 글제목은 괄호·물음표·느낌표 등 특수문자 없이 자연스러운 완성형 문장으로 작성
- 검색설명(메타 디스크립션)은 SEO를 위해 **130자 이상 150자 이내**의 밀도 있는 **3~4문장**으로 작성 (1~2문장으로 짧게 끝내지 않음)
- 마크다운 표 사용 금지(복사/붙여넣기 시 깨짐) — 대신 **🔹 소제목 + 하이픈(`-`) 목록**으로 비교/요약 정리
- 항목 사이에 `--`/`---` 구분선 삽입 금지
- 결론부에 **나만의 시선 (인사이트)** 포함 (시장·사회적 파장 분석)
- 이미지는 도입부/결론부 **정확히 2장** (각각 🎨 영문 프롬프트·📝 이미지 설명·🖼️ 대체 텍스트 세트), 민감 주제는 안전한 실사 장면(진료실·연구원 등)으로 대체, 화풍은 **고품질 실사 사진(Photorealistic)**으로 고정 (만화·벡터·일러스트 금지)
- 마지막에 **함께 찾아보면 좋은 키워드** 3~5개 제시
- 1인칭 서론은 매번 다양하고 구체적인 직업을 무작위로 설정해 현실감 부여
- **두괄식 정답 배치**: 검색설명 바로 다음(본문 최상단)에 핵심 요약 답변 1~2문장 먼저 배치 후 서론 이어감
- **Q&A 구조(AEO 최적화)**: 소제목은 대화형 질문으로 작성하고, 바로 아래 2~3문장(50단어 내외)으로 즉답
- **데이터 구조화(GEO 최적화)**: 모호한 표현 대신 확정적 어조 + 구체적 팩트·명사 위주 서술, 핵심은 불릿 리스트로 요약
- **FAQ 섹션 필수**: 본문 하단(결론 직전)에 자주 묻는 질문 2~3개를 Q&A 형태로 추가

**[한의원 모드 요건]**
- 대한민국 의료법 제56조 및 관련 규정 철저 준수
- '완치', '최고', '특효', '100% 보장' 등 과장·단정적 표현 절대 금지
- 객관적 원리·한의학적 접근·생활 관리법 중심의 차분한 정보 전달
- 본문은 일상 관리법까지만 작성하고, AI가 마무리 인사말이나 '원장님의 한마디'를 자체 생성하지 않음
- 글 맨 끝에 주소·주차 안내·홈페이지({homepage})·네이버 지도·전화번호가 담긴 **고정 푸터**를 시스템이 자동 첨부

**[🤝 AI 드림팀 모드]** (사이드바에서 선택)
- 앱이 지휘자가 되어 각 AI의 주특기대로 일을 나눠 맡김: **Gemini 리서치 → Claude 집필 → ChatGPT 검수 → (지적사항이 있으면) Claude 수정**
- Gemini는 구글 검색으로 최신 팩트 브리핑을 만들고, 작가는 브리핑에 없는 수치·날짜를 지어내지 않음
- ChatGPT 검수는 시스템이 실측한 본문 글자 수를 기준으로 분량·형식·과장 표현을 점검
- 초안 아래 **🎨 ChatGPT로 이미지 생성** 버튼으로 원고의 영문 프롬프트를 실제 이미지로 만들 수 있음
- 키가 없는 멤버는 자동으로 건너뜀 (Claude가 없으면 Gemini가 대신 집필)
""".format(homepage=CLINIC_HOMEPAGE)


DREAM_TEAM_ROLES = {
    "gemini": ("🔎 리서처", "구글 검색으로 최신 팩트 수집 · 롱테일 키워드 확장"),
    "claude": ("✍️ 메인 작가", "팩트 브리핑을 바탕으로 원고 집필 · 검수 의견 반영 수정"),
    "openai": ("🧐 검수자 · 🎨 디자이너", "작성 규칙 위반 검수 · 이미지 프롬프트로 실제 이미지 생성"),
}


def render_api_key_section(provider):
    label = PROVIDERS[provider]["label"]
    if not get_api_key(provider):
        st.text_input(f"{label} API 키", type="password", key=f"{provider}_api_key_input")
        st.button("키 적용하기", key=f"apply_key_{provider}", on_click=apply_api_key, args=(provider,))
    else:
        st.caption(f"✅ {label} API 키가 설정되어 있습니다.")
        st.button(
            "API 키 변경 및 초기화",
            key=f"reset_key_{provider}",
            on_click=reset_api_key,
            args=(provider,),
        )


def render_sidebar():
    st.subheader("작성 모드")
    st.radio(
        "작성 모드",
        [MODE_GEMINI, MODE_DREAM_TEAM],
        key="writing_mode",
        label_visibility="collapsed",
    )

    st.subheader("Gemini API 설정")
    render_api_key_section("gemini")
    if not get_api_key("gemini"):
        st.caption("뉴스 키워드 확장과 기본 글쓰기에는 Gemini API 키가 필요합니다.")

    if not is_dream_team_mode():
        st.caption("💡 '🤝 AI 드림팀' 모드를 켜면 Claude와 ChatGPT까지 함께 일합니다.")
        return

    st.divider()
    st.subheader("🤝 AI 드림팀 구성")
    for provider, (role, duty) in DREAM_TEAM_ROLES.items():
        ready = "🟢" if get_api_key(provider) else "⚪"
        st.markdown(f"{ready} **{PROVIDERS[provider]['label']}** — {role}  \n<small>{duty}</small>", unsafe_allow_html=True)
    st.caption(
        "키가 없는 멤버는 건너뛰고, 작가(Claude)가 없으면 Gemini가 대신 집필합니다. "
        "필요한 AI만 골라서 연결하면 됩니다."
    )

    st.subheader("Claude API 설정")
    render_api_key_section("claude")
    st.subheader("ChatGPT(OpenAI) API 설정")
    render_api_key_section("openai")


def render_guideline_panel():
    with st.expander("📝 현재 글쓰기 가이드라인 (프롬프트 요건)", expanded=False):
        st.markdown(GUIDELINE_TEXT)


def main():
    st.set_page_config(page_title="실시간 이슈 헤드라인", layout="wide")
    inject_compact_style()

    st.title("실시간 주요 이슈 헤드라인")

    with st.sidebar:
        render_sidebar()

    if st.button("새로고침"):
        get_news_data.clear()

    render_guideline_panel()

    tab_labels = CATEGORIES + ["한의원 글쓰기"]
    tabs = st.tabs(tab_labels)
    for tab, category in zip(tabs, CATEGORIES):
        with tab:
            with st.spinner(f"'{category}' 뉴스를 수집하고 있습니다..."):
                data = get_news_data(category)
            st.caption(f"마지막 업데이트 시간: {data['updated_at']}")
            render_headline_list(data["headlines"], tab_key=category)

    with tabs[-1]:
        render_clinic_tab()


if __name__ == "__main__":
    main()
