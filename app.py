import html
import os
import re
from difflib import SequenceMatcher
from urllib.parse import quote_plus

import streamlit as st
import feedparser
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from datetime import datetime

from homeplan import render_homeplan_master

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
# 홈판 마스터 탭에서 관점/키워드/스레드 글처럼 구조화된 목록을 받을 때 사용하는 JSON 응답 설정
GEMINI_JSON_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.8,
    max_output_tokens=8192,
    safety_settings=GEMINI_SAFETY_SETTINGS,
    thinking_config=genai_types.ThinkingConfig(thinking_budget=1024),
    response_mime_type="application/json",
)

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


def call_gemini(prompt, json_mode=False):
    config = GEMINI_JSON_CONFIG if json_mode else GEMINI_GENERATION_CONFIG
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

    col1, col2 = st.columns([8.5, 1.5], gap="small")
    with col1:
        st.subheader(draft_heading)
    with col2:
        st.button(
            "초안 지우기",
            key=f"clear_draft_{id_prefix}",
            on_click=clear_keyword,
            args=(draft_key,),
        )

    st.info("💡 출력된 결과물 우측 상단의 '📋 복사 아이콘'을 누르면 한 번에 전체 복사됩니다.")
    st.code(
        st.session_state.get(draft_key, ""),
        language="markdown",
        height=800,
        wrap_lines=True,
    )


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

    tab_labels = CATEGORIES + ["한의원 글쓰기", "홈판 마스터"]
    tabs = st.tabs(tab_labels)
    for tab, category in zip(tabs, CATEGORIES):
        with tab:
            with st.spinner(f"'{category}' 뉴스를 수집하고 있습니다..."):
                data = get_news_data(category)
            st.caption(f"마지막 업데이트 시간: {data['updated_at']}")
            render_headline_list(data["headlines"], tab_key=category)

    with tabs[-2]:
        render_clinic_tab()

    with tabs[-1]:
        render_homeplan_master(
            call_llm=call_gemini,
            get_trending=lambda: [h for h, _ in get_news_data("전체")["headlines"]],
        )


if __name__ == "__main__":
    main()
