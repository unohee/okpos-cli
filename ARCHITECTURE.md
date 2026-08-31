# ARCHITECTURE — okpos-cli

## 이 저장소는 무엇인가

OKPOS 영업정보시스템(`okasp.okpos.co.kr`)의 데이터를 자사 계정으로 수집해
Postgres에 적재하고 xlsx로 내보내는 단일 목적 CLI다.

핵심 전제 하나가 구조 전체를 결정한다: **이 시스템에는 HTML을 긁지 않아도 되는
JSON 엔드포인트가 있다.** IBSheet 그리드가 `<screen_dir>/ddd.htmlSheetAction`을
호출하며 `{"Data": [...], "Result": {"Code": 0}}`를 받는다. 따라서 이 저장소는
스크래퍼라기보다 **비공개 API 클라이언트**이고, HTML 파싱은 "어떤 파라미터를
보내야 하는가"를 알아내는 화면당 1회 작업으로 국한된다.

두 번째 전제: **수집 대상을 하드코딩하지 않는다.** `/login/menuv.jsp`가 메뉴
전체를 `var AL = [...]` JSON으로 내려주므로 이를 런타임 카탈로그로 쓴다.
벤더가 메뉴를 추가하면 코드 변경 없이 따라간다.

## 디렉터리

| 경로 | 내용 |
| --- | --- |
| `src/okpos_cli/` | 패키지 본체 (아래 모듈 표 참조) |
| `tests/` | 순수 함수 단위 테스트 — 네트워크를 타지 않는다 |
| `exports/` | xlsx 산출물 (gitignore) |
| `plan.md` | 대상 시스템 구조 조사 결과와 설계 결정의 정본 |

## 진입점

- 콘솔 스크립트 `okpos` → `okpos_cli.cli:app` (`pyproject.toml`의 `project.scripts`)
- `python -m okpos_cli` → `okpos_cli/__main__.py`

명령: `check` · `menu` · `init-db` · `scrape` · `export` · `status`

## 모듈과 의존 방향

의존은 한 방향으로만 흐른다. 아래로 갈수록 상위다.

```
config  throttle  screen        (외부 의존 없음, 순수)
   ↓       ↓        ↓
       auth                     (config + throttle)
        ↓
   catalog   client             (auth + screen)
              ↓
            shops               (client)
        ↓
     scraper        db          (client + shops + db)
        ↓
       cli          export      (전부)
```

| 모듈 | 책임 | 알아둘 것 |
| --- | --- | --- |
| `config.py` | `.env` 로딩, 로그인 URL을 base/path로 분리 | 자격증명 없으면 즉시 `SystemExit` |
| `throttle.py` | 15 RPS 토큰버킷 + lognormal 지터 | 지터는 **지연만 더한다** — 상한을 넘길 수 없다 |
| `screen.py` | 화면 HTML → 조회 스펙 | 순수 함수. 네트워크를 모른다 |
| `auth.py` | 3단계 CSRF 릴레이 로그인 | 세션 토큰(`TokenKey`/`TokenVal`) 확보까지 |
| `catalog.py` | `menuv.jsp` → `Program` 목록 | 수집 대상의 유일한 출처 |
| `shops.py` | 매장 트리 팝업 → `Shop` 목록 | 매장 코드는 메뉴가 아니라 팝업에 있다 |
| `client.py` | `SheetAction` / `DataJson` 호출 | 화면 스펙을 캐시 |
| `db.py` | 스키마 · upsert · 증분 상태 | SQL이 전부 여기 있다 |
| `scraper.py` | 카탈로그 순회 · 탭 확장 · 날짜 순회 | 오케스트레이션만, HTTP를 직접 안 한다 |
| `export.py` | JSONB → 컨트롤러별 시트 | 순수 함수 |
| `cli.py` | typer 명령 정의 | 출력 서식은 여기서만 |

## 불변식

1. **RPS 상한은 협상 대상이 아니다.** 모든 HTTP 호출은 `throttle.wait()`를 지난다.
   `auth`·`catalog`·`client` 어디서든 예외 없다. 지터는 상한을 낮추기만 한다.
2. **수집 대상은 서버가 정한다.** 화면 경로를 코드에 적지 않는다. 카탈로그와
   탭 확장 결과만 순회한다.
3. **재실행은 중복이 아니라 갱신이다.** `okpos.record`는
   `(controller, sheet_seq, shop_cd, biz_date, row_no)`가 유니크하고 upsert한다.
4. **폼 본문은 CP949다.** 모든 POST는 `auth.encode_form()`을 거친다. 응답은 UTF-8이다.
5. **성공은 확인한 것만 보고한다.** 예: `init-db`는 DDL 실행 후 실제 테이블
   목록을 조회해 셋이 다 있을 때만 성공으로 출력한다.
6. **자격증명은 저장소에 들어가지 않는다.** `.env`는 gitignore이고 `.env.example`만 추적한다.

## 데이터 모델

화면마다 컬럼이 14~69개로 제각각이고 벤더가 바꿀 수 있어, 물리 컬럼을 고정하는
대신 **메타 컬럼 + JSONB** 하이브리드를 쓴다.

- `okpos.program` — 메뉴 카탈로그
- `okpos.record` — 수집 행. 메타 컬럼으로 필터하고 `payload JSONB`(GIN)로 조회
- `okpos.scrape_run` — 조합별 상태. **증분의 유일한 근거**

## 신참을 무는 것들

- **쿠키 jar의 `#HttpOnly_` 프리픽스.** 파이썬 `MozillaCookieJar`가 이를 주석으로
  보고 세션을 통째로 잃는다. 이 저장소는 `httpx.Client`가 쿠키를 들고 있어
  해당 없지만, 디버깅용 curl jar를 파이썬으로 읽을 때 다시 밟기 쉽다.
- **`checked` 부분 문자열.** `name="unchecked_box"` 안에 `checked`가 들어 있다.
  단순 substring 검사를 쓰면 체크 안 된 박스가 조회 조건으로 제출된다.
  `screen.py`의 `_CHECKED_RE`가 속성 위치로 매칭하는 이유다.
- **CSRF 이름이 UUID다.** 필드 *이름* 자체가 매번 바뀌므로 하드코딩할 수 없고,
  로그인 각 단계마다 새로 읽어야 한다.
- **한 화면에 시트가 여러 개.** `SHEETSEQ` 1..N을 다 돌아야 데이터를 다 얻는다.
- **날짜 필드 이름이 제각각.** `date1`, `date1_1`/`date1_2`, `S_DATE`/`E_DATE`,
  `t_SALE_DATE` 등. `ScreenSpec.date_fields`가 값 형태와 이름 패턴으로 추론한다.
- **월 단위 화면은 날짜 입력이 없다.** 서버 기본값(당월)으로만 조회된다.
- **`code=-9 미등록 SQL Index`는 대개 내 요청 탓이다.** 서버가 어떤 SQL을 돌릴지
  고르는 파라미터가 비면 이 코드가 온다. 매장 트리(`shop_group_type_tree`)에서는
  `ss_SEL_GT`(형태별/그룹별)가 그 역할이고, 이 값을 빠뜨리면 `-9`가 난다.
  `<select>` 필드를 파싱에서 누락하면 정확히 이 증상이 나온다 — 서버 장애로
  단정하기 전에 form1 필드를 브라우저와 대조하라.
- **매장 목록은 메뉴에 없다.** 매장 조건이 있는 화면의 `fnCommSearchPopup4('매장',…)`
  에 박힌 `TG_INFO` 토큰을 꺼내 트리 팝업을 열어야 나온다 (`shops.py`).
- **폼 인코딩이 UTF-8이 아니다.** 페이지는 UTF-8을 선언하고 응답도 UTF-8인데
  **요청 본문만 CP949로 읽는다.** `auth.encode_form()`이 이걸 처리하므로 새 POST
  경로를 만들 때 `data=`를 직접 쓰지 말고 반드시 그 함수를 거쳐라.
- **IBSheet 컬럼명이 런타임에 조립되는 곳이 있다.** 매장 트리의
  `SaveName:"SHOP_"+ss_SEL_GT+"_NM"`가 그렇다. 정적 파싱은 `SHOP_`까지만 보므로
  `shops._resolve_columns()`가 완성한다. 잘린 이름을 보내면 그 열이 응답에서 빠진다.
- **트리 응답의 레벨은 `Level`로 판단한다.** `LEVEL_FG`는 매장 행에만 실려 오므로
  그걸로 분기하면 그룹 행을 놓친다.

## 알려진 공백

- 세션 자동 갱신이 없다. 장시간 크롤 중 만료되면 이후 조회가 `error`로 기록되고,
  증분 덕에 재실행으로 이어받는다.
- 매장 축과 날짜 축이 곱해지므로 `--all-shops`로 긴 기간을 돌리면 조회 수가
  빠르게 커진다 (매장별 화면 44개 × 16매장 × N일). 15 RPS 상한 아래에서
  1일치가 수 분이다.
- 병렬 수집을 하지 않는다. `HumanThrottle`은 락을 들고 있어 병렬화에 대비돼
  있으나, 현재 `scraper`는 순차다.

## CI

`unohee/ci-templates`의 재사용 워크플로를 호출한다
(`.github/workflows/python-ci.yml`). 공통 규칙은 템플릿에서 바꾸고, 이 저장소의
예외는 caller의 입력으로 표현한다.
