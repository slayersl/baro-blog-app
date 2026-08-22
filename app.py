import html
import os
import re
from difflib import SequenceMatcher
from urllib.parse import quote_plus

import streamlit as st
import feedparser
from st_copy_to_clipboard import st_copy_to_clipboard
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
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
당신은 월 천만 원의 수익을 내는 상위 1% 블로그 마케터입니다.
주어진 뉴스 헤드라인 '{headline}'을(를) 바탕으로 SEO에 완벽하게 최적화된 고품질 블로그 포스팅 원고를 작성하세요.

[필수 조건]
- 본문 내에 핵심 키워드를 3~5회 자연스럽게 반복하세요.
- {keyword_instruction}
- 주의사항: 타겟 키워드가 긴 문장이나 특수기호가 포함된 기사 헤드라인일 경우, 본문에 그 원문을 토씨 하나 틀리지 않고 기계적으로 반복 삽입하지 마세요. 핵심 명사 단어(예: 암 백신, 상용화 등) 위주로 자연스럽게 분리 및 변형하여 문맥에 부드럽게 녹여내고, 읽었을 때 로봇이 쓴 것 같은 어색한 문장이 절대 발생하지 않도록 '자연스러운 한국어 문맥'을 최우선으로 작성하세요.
- 절대 아래의 [출력 템플릿] 중 단 하나의 항목도 누락해서는 안 됩니다. 명시된 기호(📌)와 순서를 정확히 지켜서 출력하세요.
- 본문 내 소제목에는 마크다운 샵 기호(##, ###)를 절대 쓰지 마세요. 대신 모든 소제목 앞에는 '✨' 기호를 붙여서 작성하세요. (예: ✨ '암 백신' 최종테스트 통과 소식이 글로벌 바이오 시장에서 왜 큰 화제인가요?)
- 본문에 표(|:---|:---| 형태의 마크다운 표)는 블로그에서 깨지므로 절대 생성하지 마세요. 핵심 비교나 조건 요약이 필요할 때는 아래처럼 들여쓰기와 하이픈(-)을 활용한 리스트로 작성하세요.
  ✨ [비교/핵심 요약]
    - 항목 1: 설명 내용
    - 항목 2: 설명 내용
- 검색설명은 검색 엔진 최적화(SEO)를 위해 반드시 130자 이상, 150자 이내의 길이로 구체적이고 풍부하게 작성하세요.
- 각 항목(검색설명, 본문 등) 사이에 `--` 나 `---` 같은 하이픈/대시 구분선을 절대 임의로 삽입하지 마세요. 불필요한 공백이나 선 없이 바로 다음 📌 기호로 연결하세요.
- 이미지 프롬프트는 구글 제미나이(Imagen) 이미지 생성에 최적화된 상세하고 자연스러운 영문 프롬프트로 작성하세요. 사람이 등장하는 경우 서양인이나 왜곡된 외모가 절대 나오지 않도록 반드시 "A photorealistic portrait of an attractive and friendly modern Korean man/woman with natural, clean facial features, high quality" 같은 문구를 영문 프롬프트에 포함하세요.

[GEO & AEO 최적화 규칙]
- 각 소제목(✨)은 사용자가 실제 검색할 법한 '질문형 문장'으로 작성하세요.
- 소제목 바로 다음 첫 문단은 서론 없이, 질문에 대한 핵심 답변을 2~3문장(50단어 내외)으로 즉시 요약해서 제공하세요 (Featured Snippet 타겟).
- 추상적인 표현을 피하고 구체적인 수치, 명사, 팩트 중심으로 서술하여 AI 검색 엔진의 신뢰도(Information Gain)를 높이세요.
- 정보를 나열할 때는 긴 문단 대신 가독성 높은 짧은 불릿 리스트(하이픈 -)로 구조화하세요. ChatGPT, Perplexity, 구글 AI Overviews가 답변의 출처로 인용하기 좋도록 작성하세요.

[출력 템플릿]
📌 **키워드:** (가장 검색량이 많을 만한 핵심 키워드 1개)
📌 **글제목:** (기존 헤드라인을 쓰지 말고, 키워드를 포함하여 새로 창작한 클릭 유도형 제목)
📌 **검색설명:** (본문 최상단에 넣을 메타 디스크립션. SEO를 위해 반드시 130자 이상 150자 이내로 구체적이고 풍부하게 작성)
📌 **본문:**
(도입부, 사건의 재구성, 파장, 요약 등 약 1500자 분량. 독자의 궁금증을 해소하는 해요체 사용. 소제목은 ✨로 표시)
📌 **나만의 시선 (인사이트):** (이 이슈가 시장이나 사회에 미칠 파장을 분석하여, 본문의 자연스러운 마무리 결론 역할을 하도록 작성. 테마주 등 심층 분석 300자)
📌 **함께 찾아보면 좋은 키워드:** (관련 검색어 3개)
📌 **이미지 프롬프트:** (구글 제미나이(Imagen) 이미지 생성에 최적화된, 상세하고 자연스러운 영문 프롬프트로 작성. 화풍 제한 없이 최고 퀄리티로 작성하되, 사람이 등장하는 경우 서양인이나 왜곡된 외모가 절대 나오지 않도록 반드시 "A photorealistic portrait of an attractive and friendly modern Korean man/woman with natural, clean facial features, high quality" 같은 문구를 영문 프롬프트에 포함하여 세련된 한국인 미남·미녀 비주얼의 고품질 썸네일이 되도록 작성할 것)
📌 **이미지 설명:** (글 본문에 들어갈 사진 캡션 1줄)
📌 **대체텍스트:** (시각장애인과 검색 로봇이 이미지를 정확히 이해할 수 있도록 묘사하는 대체텍스트 1줄)
📌 **라벨:** (글의 핵심 주제 키워드 3~5개를 쉼표(,)로 구분하여 출력. 예: 암 백신, 바이오테크, 임상결과)
📌 **퍼머링크:** (구글 검색 최적화(SEO)를 위한 영어 URL 슬러그. 핵심 주제를 띄어쓰기 없이 영문 소문자와 하이픈(-)만 사용하여 작성. 예: cancer-vaccine-final-test-success)
"""

KEYWORD_EXPANSION_PROMPT_TEMPLATE = """사용자가 입력한 짧은 키워드 '{keyword}'를 바탕으로, 구글/네이버 검색 유입 확률이 높고 대형 블로그와 경쟁을 피할 수 있는 구체적인 질문형/해결형 '롱테일 키워드' 5개를 추천해 주세요. 각 키워드별로 검색 의도의 명확성과 경쟁도 낮음을 종합 평가한 'SEO 매력도 점수(100점 만점)'와 '추천 이유(1줄)'를 작성하세요. 출력은 반드시 마크다운 표(Table) 형식으로 작성하세요. 컬럼: | 추천 롱테일 키워드 | SEO 점수 | 추천 이유 |"""

CLINIC_PROMPT_TEMPLATE = f"""
당신은 한의원 블로그 마케팅을 전문으로 하는 카피라이터입니다.
주어진 증상/주제 '{{topic}}'에 대해 환자들이 검색할 만한 정보를 바탕으로 SEO에 최적화된 한의원 블로그 포스팅 원고를 작성하세요.

[철저한 의료법 방어 규칙]
- 대한민국 의료법 제56조 및 관련 규정을 철저히 준수합니다.
- '완치', '최고', '특효', '부작용 0%', '100% 보장' 등 보건소, 식약처, 경찰서 등 유관기관의 단속이나 민원이 들어올 수 있는 과장되거나 단정적인 표현, 효능·효과 과대광고를 절대 금지합니다.
- 질환의 객관적인 원리, 한의학적 접근법, 일상생활 관리법 등을 차분하고 신뢰감 있는 정보 전달 목적으로만 서술하세요.
- 병원 주소·전화번호·홈페이지 등 상세 연락처는 시스템이 별도의 고정 푸터로 자동 첨부하므로, 본문에서 직접 나열하지 마세요.

[필수 조건]
- 본문 내에 핵심 키워드를 3~5회 자연스럽게 반복하세요.
- 주의사항: 타겟 키워드가 긴 문장이나 특수기호가 포함된 기사 헤드라인일 경우, 본문에 그 원문을 토씨 하나 틀리지 않고 기계적으로 반복 삽입하지 마세요. 핵심 명사 단어(예: 암 백신, 상용화 등) 위주로 자연스럽게 분리 및 변형하여 문맥에 부드럽게 녹여내고, 읽었을 때 로봇이 쓴 것 같은 어색한 문장이 절대 발생하지 않도록 '자연스러운 한국어 문맥'을 최우선으로 작성하세요.
- 주의: 본문은 일상 수칙(관리법)까지만 작성하고, 절대 마무리 인사말이나 '원장님의 한마디'를 자체적으로 생성하지 마세요. 본문 작성이 끝나면 즉시 '📌 함께 찾아보면 좋은 키워드' 항목으로 넘어가세요.
- 절대 아래의 [출력 템플릿] 중 단 하나의 항목도 누락해서는 안 됩니다. 명시된 기호(📌)와 순서를 정확히 지켜서 출력하세요.
- 본문 내 소제목에는 마크다운 샵 기호(##, ###)를 절대 쓰지 마세요. 대신 모든 소제목 앞에는 '✨' 기호를 붙여서 작성하세요.
- 본문에 표(|:---|:---| 형태의 마크다운 표)는 블로그에서 깨지므로 절대 생성하지 마세요. 핵심 비교나 관리법 요약이 필요할 때는 들여쓰기와 하이픈(-)을 활용한 리스트로 작성하세요.
- 검색설명은 검색 엔진 최적화(SEO)를 위해 반드시 130자 이상, 150자 이내의 길이로 구체적이고 풍부하게 작성하세요.
- 각 항목 사이에 `--` 나 `---` 같은 하이픈/대시 구분선을 절대 임의로 삽입하지 마세요. 불필요한 공백이나 선 없이 바로 다음 📌 기호로 연결하세요.
- 이미지 프롬프트는 구글 제미나이(Imagen) 이미지 생성에 최적화된 상세하고 자연스러운 영문 프롬프트로 작성하세요. 사람이 등장하는 경우 서양인이나 왜곡된 외모가 절대 나오지 않도록 반드시 "A photorealistic portrait of an attractive and friendly modern Korean man/woman with natural, clean facial features, high quality" 같은 문구를 영문 프롬프트에 포함하세요.

[GEO & AEO 최적화 규칙]
- 각 소제목(✨)은 환자가 실제 검색할 법한 '질문형 문장'으로 작성하세요.
- 소제목 바로 다음 첫 문단은 서론 없이, 질문에 대한 핵심 답변을 2~3문장(50단어 내외)으로 즉시 요약해서 제공하세요 (Featured Snippet 타겟).
- 추상적인 표현을 피하고 구체적인 수치, 명사, 팩트 중심으로 서술하여 AI 검색 엔진의 신뢰도(Information Gain)를 높이세요.
- 정보를 나열할 때는 긴 문단 대신 가독성 높은 짧은 불릿 리스트(하이픈 -)로 구조화하세요.

[출력 템플릿]
📌 **키워드:** (가장 검색량이 많을 만한 핵심 키워드 1개)
📌 **글제목:** (증상/주제를 반영해 새로 창작한 클릭 유도형 제목)
📌 **검색설명:** (본문 최상단에 넣을 메타 디스크립션. SEO를 위해 반드시 130자 이상 150자 이내로 구체적이고 풍부하게 작성)
📌 **본문:**
(원인, 증상 설명, 한의학적 접근, 생활 관리법 등 약 1500자 분량. 독자의 궁금증을 해소하는 해요체 사용. 소제목은 ✨로 표시. 마무리 인사말이나 원장님의 한마디는 쓰지 않고 관리법에서 바로 끝낼 것)
📌 **함께 찾아보면 좋은 키워드:** (관련 검색어 3개)
📌 **이미지 프롬프트:** (구글 제미나이(Imagen) 이미지 생성에 최적화된, 상세하고 자연스러운 영문 프롬프트로 작성. 화풍 제한 없이 최고 퀄리티로 작성하되, 사람이 등장하는 경우 서양인이나 왜곡된 외모가 절대 나오지 않도록 반드시 "A photorealistic portrait of an attractive and friendly modern Korean man/woman with natural, clean facial features, high quality" 같은 문구를 영문 프롬프트에 포함하여 세련된 한국인 미남·미녀 비주얼의 고품질 썸네일이 되도록 작성할 것)
📌 **이미지 설명:** (글 본문에 들어갈 사진 캡션 1줄)
📌 **대체텍스트:** (시각장애인과 검색 로봇이 이미지를 정확히 이해할 수 있도록 묘사하는 대체텍스트 1줄)
📌 **라벨:** (글의 핵심 주제 키워드 3~5개를 쉼표(,)로 구분하여 출력. 예: 암 백신, 바이오테크, 임상결과)
📌 **퍼머링크:** (구글 검색 최적화(SEO)를 위한 영어 URL 슬러그. 핵심 주제를 띄어쓰기 없이 영문 소문자와 하이픈(-)만 사용하여 작성. 예: cancer-vaccine-final-test-success)
"""


def get_gemini_api_key():
    # 사이드바에서 직접 입력/초기화한 키를 항상 최우선으로 사용한다.
    session_key = st.session_state.get("gemini_api_key", "")
    if session_key:
        return session_key

    # '초기화' 버튼을 누른 직후에는 .env/secrets 값이 남아있어도 무시하고
    # 빈 입력창을 보여줘야 하므로, 여기서 즉시 빈 값을 반환한다.
    if st.session_state.get("ignore_env_key"):
        return ""

    env_key = os.getenv("GEMINI_API_KEY")
    if env_key:
        return env_key

    try:
        secrets_key = st.secrets.get("GEMINI_API_KEY")
    except Exception:
        secrets_key = None
    if secrets_key:
        return secrets_key

    return ""


GEMINI_TIMEOUT_MS = 60_000  # 네트워크가 응답 없이 무한정 대기하지 않도록 60초 타임아웃 강제


@st.cache_resource(show_spinner=False)
def get_gemini_client(api_key):
    return genai.Client(
        api_key=api_key,
        http_options=genai.types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )


def call_gemini(prompt):
    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Gemini API 키가 설정되지 않았습니다. 사이드바에서 입력해주세요.")

    client = get_gemini_client(api_key)

    try:
        try:
            response = client.models.generate_content(model=GEMINI_MODEL_PRIMARY, contents=prompt)
        except genai_errors.APIError:
            # gemini-3.6-pro를 사용할 수 없는 계정/리전인 경우 gemini-3.6-flash로 폴백
            response = client.models.generate_content(model=GEMINI_MODEL_FALLBACK, contents=prompt)
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


def generate_blog_post(headline, target_keyword=""):
    target_keyword = (target_keyword or "").strip()
    if target_keyword:
        keyword_instruction = (
            f"반드시 사용자가 지정한 키워드 '{target_keyword}'을(를) 메인 타겟 키워드로 사용하여 "
            "글을 작성하세요. 임의로 다른 키워드를 추출하지 마세요."
        )
    else:
        keyword_instruction = "뉴스 헤드라인을 분석해 롱테일 키워드를 1개 자동 추출하세요."

    prompt = BLOG_PROMPT_TEMPLATE.format(headline=headline, keyword_instruction=keyword_instruction)
    return call_gemini(prompt)


def expand_keyword(keyword):
    prompt = KEYWORD_EXPANSION_PROMPT_TEMPLATE.format(keyword=keyword)
    return call_gemini(prompt)


def generate_clinic_post(topic):
    prompt = CLINIC_PROMPT_TEMPLATE.format(topic=topic)
    text = call_gemini(prompt)
    # 주소/전화번호 등은 AI가 매번 다시 쓰게 하지 않고, 고정 푸터를 그대로 이어붙인다.
    return f"{text}\n\n{CLINIC_FOOTER_TEMPLATE}"


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


def reset_api_key():
    st.session_state["gemini_api_key"] = ""
    st.session_state["ignore_env_key"] = True
    if "gemini_api_key_input" in st.session_state:
        del st.session_state["gemini_api_key_input"]


def apply_api_key():
    # 위젯 자신의 key(gemini_api_key_input)에서 값을 읽어와, 별도의
    # 세션 변수(gemini_api_key)에 확정적으로 덮어쓴다. text_input의 반환값을
    # 매 rerun마다 암묵적으로 미러링하던 기존 방식은 붙여넣기 타이밍에 따라
    # 값이 반영되지 않을 수 있어, 명시적인 "적용" 버튼 클릭 시점에만 확정한다.
    st.session_state["gemini_api_key"] = st.session_state.get("gemini_api_key_input", "").strip()
    st.session_state["ignore_env_key"] = False
    st.rerun()


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
        col_input, col_expand, col_write, col_copy, col_clear = st.columns(
            [4, 1.3, 1.6, 1.1, 1.1], gap="small"
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
        with col_copy:
            st_copy_to_clipboard(
                text=st.session_state.get(keyword_key, ""),
                before_copy_label="복사",
                after_copy_label="복사 완료!",
                key=f"copy_kw_{id_prefix}",
            )
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
            try:
                with st.spinner(spinner_text):
                    st.session_state[draft_key] = generate_fn(current_keyword)
            except Exception as e:
                st.error(str(e))

    if st.session_state[expansion_key]:
        render_keyword_suggestions(st.session_state[expansion_key], keyword_key, id_prefix)

    # 추천 키워드 표의 "글쓰기" 버튼이 눌리면(select_and_trigger_write 콜백이 세운 플래그),
    # 사용자가 '키워드로 글쓰기'를 따로 누르지 않아도 여기서 바로 이어서 생성한다.
    trigger_key = f"auto_write_{id_prefix}"
    if st.session_state.get(trigger_key):
        st.session_state[trigger_key] = False
        current_keyword = st.session_state.get(keyword_key, "").strip()
        if current_keyword:
            try:
                with st.spinner(spinner_text):
                    st.session_state[draft_key] = generate_fn(current_keyword)
            except Exception as e:
                st.error(str(e))

    col1, col2, col3 = st.columns([7, 1.5, 1.5], gap="small")
    with col1:
        st.subheader(draft_heading)
    with col2:
        st_copy_to_clipboard(
            text=st.session_state.get(draft_key, ""),
            before_copy_label="전체 복사",
            after_copy_label="복사 완료!",
            key=f"copy_all_{id_prefix}",
        )
    with col3:
        st.button(
            "초안 지우기",
            key=f"clear_draft_{id_prefix}",
            on_click=clear_keyword,
            args=(draft_key,),
        )

    st.text_area(
        f"{draft_heading} 결과",
        height=800,
        key=draft_key,
        label_visibility="collapsed",
    )


def render_headline_list(headlines, tab_key):
    # 이 키를 st.session_state 저장소이자 아래 st.text_area의 위젯 key로 동시에 사용한다.
    # (별도의 value=를 text_area에 넘기면, 위젯이 이미 한 번 생성된 뒤에는 Streamlit이
    #  새 value를 무시하고 자기 자신의 위젯 상태만 유지해 "생성은 되는데 화면엔 안 뜨는"
    #  현상이 생긴다. key와 저장 위치를 일치시켜야 갱신이 안전하게 반영된다.)
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
            col_text, col_copy, col_kw, col_blog = st.columns([3.2, 1.4, 1.9, 1.6], gap="small")
            with col_text:
                st.markdown(f"**{rank}.** {headline}")
            with col_copy:
                st_copy_to_clipboard(
                    text=headline,
                    before_copy_label="복사하기",
                    after_copy_label="복사 완료!",
                    key=f"copy_{tab_key}_{rank}",
                )
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
                    "블로그 글쓰기(1500자)",
                    key=f"blog_{tab_key}_{rank}",
                    on_click=set_keyword_value,
                    args=(keyword_key, headline),
                ):
                    try:
                        target_keyword = st.session_state.get(keyword_key, "")
                        with st.spinner("AI가 블로그 초안을 작성하고 있습니다..."):
                            result = generate_blog_post(headline, target_keyword=target_keyword)
                        st.session_state[draft_key] = result
                    except Exception as e:
                        st.error(str(e))
            st.html('<hr class="headline-sep">')

    render_keyword_and_draft_panel(
        id_prefix=tab_key,
        keyword_key=keyword_key,
        expansion_key=expansion_key,
        draft_key=draft_key,
        generate_fn=lambda kw: generate_blog_post(kw, target_keyword=kw),
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
- 소제목 기호: 마크다운 `##`/`###` 대신 **✨** 사용
- 검색설명(메타 디스크립션)은 SEO를 위해 **130자 이상 150자 이내**로 구체적이고 풍부하게 작성
- 표 대신 들여쓰기 + 하이픈(`-`) 목록으로 비교/요약 정리
- 항목 사이에 `--`/`---` 구분선 삽입 금지
- 결론부에 **나만의 시선 (인사이트)** 포함 (시장·사회적 파장 분석)
- 이미지 프롬프트는 제미나이(Imagen)용 영문 프롬프트, 한국인 비주얼 문구 필수 포함
- 마지막에 **함께 찾아보면 좋은 키워드** 3개 제시
- **Q&A 구조(AEO 최적화)**: 소제목은 대화형 질문으로 작성하고, 바로 아래 2~3문장(50단어 내외)으로 즉답
- **데이터 구조화(GEO 최적화)**: 구체적인 팩트·명사 위주 서술 및 짧은 리스트 형태로 AI 가독성 극대화

**[한의원 모드 요건]**
- 대한민국 의료법 제56조 및 관련 규정 철저 준수
- '완치', '최고', '특효', '100% 보장' 등 과장·단정적 표현 절대 금지
- 객관적 원리·한의학적 접근·생활 관리법 중심의 차분한 정보 전달
- 본문은 일상 관리법까지만 작성하고, AI가 마무리 인사말이나 '원장님의 한마디'를 자체 생성하지 않음
- 글 맨 끝에 주소·주차 안내·홈페이지({homepage})·네이버 지도·전화번호가 담긴 **고정 푸터**를 시스템이 자동 첨부
""".format(homepage=CLINIC_HOMEPAGE)


def render_guideline_panel():
    with st.expander("📝 현재 글쓰기 가이드라인 (프롬프트 요건)", expanded=False):
        st.markdown(GUIDELINE_TEXT)


def main():
    st.set_page_config(page_title="실시간 이슈 헤드라인", layout="wide")
    inject_compact_style()

    st.title("실시간 주요 이슈 헤드라인")

    with st.sidebar:
        st.subheader("Gemini API 설정")
        if not get_gemini_api_key():
            st.text_input(
                "Gemini API 키", type="password", key="gemini_api_key_input"
            )
            st.button("키 적용하기", on_click=apply_api_key)
            st.caption("블로그 글쓰기 기능을 쓰려면 Gemini API 키가 필요합니다.")
        else:
            st.caption("Gemini API 키가 설정되어 있습니다.")
            st.button("API 키 변경 및 초기화", on_click=reset_api_key)

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
