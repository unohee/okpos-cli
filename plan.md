# okpos-cli — 구현 계획

작성: 2026-08-31 · 상태: 스캐폴딩 착수

## 목표
OKPOS 영업정보시스템(okasp.okpos.co.kr)의 전체 데이터를 CLI로 수집해
Postgres에 적재하고 xlsx로 내보낸다.

## 실측으로 확인된 대상 시스템 구조 (2026-08-31)

### 인증 — 3단계 릴레이 + 세션 토큰
1. `GET /login/login_form.jsp` → hidden `<uuid name>=<uuid value>` CSRF 쌍 (매 요청 재생성)
2. `POST /login/login_check.jsp` (user_id, user_pwd, AutoFg=W, CSRF) → **새 CSRF** 포함 중간 폼 반환
3. `POST /login/login_check_action.jsp` (동일 필드 + 새 CSRF) → JSESSIONID 확립,
   성공 시 body에 `top.location.replace('/login/top_frame.jsp')`
4. `GET /login/top_frame.jsp` → `TokenKey`/`TokenVal` (세션 내내 고정, 모든 조회에 첨부)

### 카탈로그
`GET /login/menuv.jsp` → `var AL = [...]` JSON. 71개 프로그램.
필드: PGM_CD, PGM_FILE_NM, PGM_LCLS_NM/PGM_MCLS_NM/PGM_NM, HTML_OPTION.

### 데이터 API (범용 — HTML 파싱 불필요)
`POST <screen_dir>/ddd.htmlSheetAction`
- 필수: `<TokenKey>=<TokenVal>`, `S_CONTROLLER`, `S_METHOD=search`, `SHEETSEQ`,
  `S_SAVENAME`(IBSheet SaveName 파이프 결합), `S_ORDERBY`
- 조회조건: 해당 화면 form1의 필드 전체 (date1, ss_SHOP_CD, ss_POS_NO ...)
- 응답: `{"Etc":..,"Data":[{...}],"Result":{"Message":"조회완료","Code":0}}`
- Code 0 성공 / -9 오류(미등록 SQL Index 등)

보조: `POST /common/jsp/ajax/DataJson.jsp` — `sp_info`(암호화 토큰), `sp_params`(⊥ 구분),
`strSaveName`. 콤보/코드 목록용.

### 화면 2종
- **직접형**: `<form id='form1'>` 존재 → 그대로 조회
- **탭형**: `myTab1LoadForm` + `IBS_InitTab(myTab1, title, url)` →
  하위 JSP 목록을 정규식으로 추출해 각각 직접형으로 처리
  (예: day_jump010 → day_total010, day_shop010, day_time010 … 12개)

### 함정
- 쿠키 jar의 `#HttpOnly_` 프리픽스를 `MozillaCookieJar`가 주석 처리 → 세션 유실
- 한 화면에 시트가 여러 개 (SHEETSEQ 1..N). day_detail010은 2개
- 일부 화면은 `ss_SHOP_CD` 필수
- 따옴표가 화면마다 홑/쌍 혼재 → 속성 파서는 둘 다 처리

### 검증된 실데이터
2026-08-25 `sale.sale.day_summery010`:
검증에는 실제 업무 데이터를 사용했으며 값은 공개 저장소에 포함하지 않는다.

## 확정된 설계 결정 (사용자 승인 2026-08-31)
1. **범위**: 71개 전체 자동 크롤 (메뉴 JSON을 런타임 카탈로그로 사용)
2. **스키마**: 하이브리드 — 메타 컬럼 + `payload JSONB`
3. **모드**: 날짜 범위 + 증분 (수집 완료 조합은 스킵, 상태는 Postgres 기록)

## 명시적 가정
- A1. "np.rand 가미"는 요청 간격에 사람 흉내 랜덤 지터를 넣으라는 뜻으로 해석.
  numpy lognormal + 간헐적 긴 휴식(사람의 화면 읽는 시간)으로 구현.
- A2. 15RPS는 **상한**이며 평균은 그보다 낮게 유지 (토큰버킷 + 지터).
- A3. 자격증명은 `.env`의 OKPOS_ID/OKPOS_PW/OKPOS_URL 사용. 저장소에 커밋하지 않음.
- A4. 대상은 사용자 본인 계정([ACCOUNT] 예시 회사)의 자사 데이터.

## 스택
Python 3.12 / httpx / typer + rich / psycopg[binary] (SQLAlchemy 미사용 — 테이블 소수)
/ openpyxl / numpy / python-dotenv

## 단계 + 검증
1. 스캐폴딩(pyproject, src-layout, .gitignore, .env.example)
   → 검증: `tomllib.load` 파싱 성공, `pip install -e .` 성공
2. auth/throttle/catalog/screen/client 구현
   → 검증: 실제 로그인 후 menuv 71개 파싱, day_summery010에서 8/25 매출 재현
3. db(스키마+upsert) 구현
   → 검증: 로컬 Postgres(docker) 스키마 생성 + 실제 행 적재 후 SELECT 확인
4. scraper(증분) 구현
   → 검증: 동일 명령 2회 실행 시 2회차가 스킵되는지 확인
5. export(xlsx) 구현
   → 검증: 생성된 xlsx를 openpyxl로 재오픈해 행 수 대조
6. git 커밋
   → 검증: `git log --oneline -1`, `git ls-files`

## 동시성 검토 대상
- 토큰버킷: 단일 프로세스 순차 요청이므로 스레드 경합 없음. 병렬화 시 락 필요 (현재 미도입)
- 세션: JSESSIONID 1개를 순차 사용. 세션 만료 시 재로그인 재진입 필요
- DB: upsert는 (program_cd, shop_cd, biz_date, sheet_seq) 유니크 제약 기반 → 재실행 안전
