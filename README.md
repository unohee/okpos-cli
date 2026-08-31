# okpos-cli

OKPOS 영업정보시스템(`okasp.okpos.co.kr`)에 로그인해 **전체 데이터를 수집**하고
Postgres에 적재하거나 xlsx로 내보내는 CLI.

자사 계정으로 접근 가능한 데이터를 대상으로 하며, 요청은 15 RPS 상한 아래에서
사람과 비슷한 간격으로 나갑니다.

## 왜 HTML을 긁지 않는가

조사 결과 이 시스템은 IBSheet 그리드 뒤에 **JSON 엔드포인트**를 두고 있습니다.

```
POST <screen_dir>/ddd.htmlSheetAction
  <TokenKey>=<TokenVal>, S_CONTROLLER, S_METHOD=search,
  SHEETSEQ, S_SAVENAME, S_ORDERBY, + 해당 화면의 조회 조건
→ {"Data": [ ... ], "Result": {"Message": "조회완료", "Code": 0}}
```

따라서 화면 HTML은 **파라미터 이름을 알아내기 위해 화면당 한 번만** 읽고,
실제 데이터 행은 전부 JSON으로 받습니다. 스크래핑이라기보다 비공개 API 호출에
가깝고, 마크업이 바뀌어도 잘 깨지지 않습니다.

수집 대상 목록도 하드코딩하지 않습니다. `/login/menuv.jsp`가 메뉴 전체를
`var AL = [...]` JSON으로 내려주므로 이를 런타임 카탈로그로 사용합니다.
현재 71개 프로그램이며, 벤더가 메뉴를 추가하면 자동으로 따라갑니다.

## 설치

```bash
python3 -m venv ~/dev/venvs/okpos-cli
~/dev/venvs/okpos-cli/bin/pip install -e ".[dev]"
```

`.env.example`을 `.env`로 복사해 채웁니다.

```ini
OKPOS_ID=...
OKPOS_PW=...
OKPOS_URL=https://okasp.okpos.co.kr/login/login_form.jsp
OKPOS_PG_DSN=postgresql://user:pass@localhost:5432/okpos   # 선택
OKPOS_MAX_RPS=15                                           # 선택
```

`.env`는 `.gitignore`에 있습니다. 커밋하지 마십시오.

## 사용법

```bash
okpos check                                   # 로그인·카탈로그·요청 속도 확인
okpos menu --class 매출관리                    # 프로그램 목록
okpos init-db                                 # Postgres 스키마 생성

okpos scrape --from 2026-08-25                        # 하루치 전체 수집
okpos scrape --from 2026-08-01 --to 2026-08-31        # 기간 수집 (증분)
okpos scrape --from 2026-08-25 --dry-run --no-db      # 조회 계획만 확인
okpos scrape --from 2026-08-25 --class 매출관리 --full  # 특정 분류를 강제 재수집

okpos status                                  # 수집 현황
okpos export --from 2026-08-01 --to 2026-08-31 --out exports/aug.xlsx
```

기본은 **증분**입니다. 성공 기록이 있는 (컨트롤러, 시트, 매장, 날짜) 조합은
건너뛰므로 매일 크론으로 돌려도 안전합니다. `--full`은 이를 무시합니다.

## 요청 페이싱

두 층이 겹쳐 있습니다.

1. **토큰 버킷** — 15 RPS 하드 상한. 서버와의 약속이며 생략되지 않습니다.
2. **lognormal 지터** — 사람이 화면을 넘기는 듯한 간격, 낮은 확률로 더 긴 휴식.
   지터는 **항상 지연을 더하기만** 하므로 상한을 넘길 수 없습니다.

`scrape` 실행 후 실측 피크 RPS가 함께 출력됩니다.

## 데이터 모델

화면마다 컬럼이 14~69개로 제각각이고 벤더가 바꿀 수 있으므로, 물리 컬럼을
고정하는 대신 **메타 컬럼 + JSONB** 하이브리드를 씁니다.

| 테이블 | 용도 |
| --- | --- |
| `okpos.program` | 메뉴 카탈로그 |
| `okpos.record` | 수집된 행 (`payload JSONB` + 메타 컬럼, GIN 인덱스) |
| `okpos.scrape_run` | 조합별 수집 상태 — 증분의 근거 |

`record`는 `(controller, sheet_seq, shop_cd, biz_date, row_no)`가 유니크하므로
재실행은 중복이 아니라 갱신입니다.

xlsx로 내보낼 때는 JSONB를 컨트롤러별 시트의 컬럼으로 다시 펼치므로,
원래 OKPOS 그리드와 비슷한 모양이 됩니다.

## 실측 결과 (2026-08-31)

| 항목 | 값 |
| --- | --- |
| 카탈로그 | 71개 프로그램 |
| 조회 가능 화면 | 70개 (탭 화면 확장 포함) |
| 1일치 수집 | <조회 수> / <행 수> / <컨트롤러 수> |
| 실측 피크 | 9 RPS (상한 15) |
| 재실행 | 101건 스킵, 실패분만 재시도 |

## 알려진 한계

- `sale.cust.day_sale020`(회원포인트실적)은 서버가 `code=-9 미등록 SQL Index`로
  응답합니다. 서버측 문제이며 `scrape_run`에 `error`로 기록됩니다.
- `/sale/day/day_cuprf010.jsp`(컵보증금)는 HTTP 오류로 해석되지 않습니다.
  계정 권한 문제로 보입니다.
- 월 단위 화면(`sale.month.*`)은 날짜 입력 필드가 없어 서버 기본값(당월)으로
  조회됩니다. 임의 월을 지정하려면 해당 화면의 파라미터를 따로 다뤄야 합니다.
- 매장 스코프가 필요한 화면은 `--shop <코드>`로 지정합니다. 미지정 시 계정
  기본 범위로 조회됩니다.
- **세션 자동 갱신이 없습니다.** 한 번 로그인한 JSESSIONID로 끝까지 돕니다.
  1일치가 약 2분이므로 긴 기간을 한 번에 돌리면 도중에 세션이 만료될 수 있고,
  그 시점부터의 조회는 `scrape_run`에 `error`로 남습니다. 증분 덕분에 다시
  실행하면 남은 것만 이어서 수집되므로, 긴 기간은 며칠씩 나눠 도는 편이
  안전합니다.

## 개발

```bash
~/dev/venvs/okpos-cli/bin/python -m pytest tests -q
~/dev/venvs/okpos-cli/bin/ruff check src tests
```

설계 배경과 대상 시스템 구조는 `plan.md`에 정리돼 있습니다.
