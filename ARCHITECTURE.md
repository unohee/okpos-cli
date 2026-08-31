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
     scraper        db          (client + db)
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
4. **성공은 확인한 것만 보고한다.** 예: `init-db`는 DDL 실행 후 실제 테이블
   목록을 조회해 셋이 다 있을 때만 성공으로 출력한다.
5. **자격증명은 저장소에 들어가지 않는다.** `.env`는 gitignore이고 `.env.example`만 추적한다.

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

## 알려진 공백

- 세션 자동 갱신이 없다. 장시간 크롤 중 만료되면 이후 조회가 `error`로 기록되고,
  증분 덕에 재실행으로 이어받는다.
- 병렬 수집을 하지 않는다. `HumanThrottle`은 락을 들고 있어 병렬화에 대비돼
  있으나, 현재 `scraper`는 순차다.

## CI

`unohee/ci-templates`의 재사용 워크플로를 호출한다
(`.github/workflows/python-ci.yml`). 공통 규칙은 템플릿에서 바꾸고, 이 저장소의
예외는 caller의 입력으로 표현한다.
