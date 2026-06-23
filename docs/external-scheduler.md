# 외부 스케줄러로 정시 실행하기 (GitHub Actions dispatch)

## 왜 필요한가

GitHub Actions의 `schedule`(cron) 트리거는 **정시 실행을 보장하지 않는다.** 공식 문서에도
"높은 부하 시간대(특히 매시 정각)에는 지연되거나 건너뛸 수 있다"고 명시돼 있다. 그래서
08:30에 걸어둬도 09:00 단타 구간을 놓치고 10시 넘어 시작되는 일이 잦았다.

해결책: **`schedule`을 제거하고**, 외부의 정확한 스케줄러가 정해진 시각에 GitHub API로
`workflow_dispatch`를 호출하게 한다. dispatch로 들어온 실행은 일반 push처럼 처리돼
**거의 즉시(수초~1분) 러너가 붙는다.** 컴퓨팅은 그대로 GitHub Actions(무료)에 남는다.

## 1) GitHub 토큰 발급 (Fine-grained PAT 권장)

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
2. **Repository access**: Only select repositories → `asitwas729/auto-trade`
3. **Permissions** → Repository permissions → **Actions: Read and write**
4. 만료일 설정 후 생성 → 토큰 문자열 복사 (한 번만 보임)

## 2) 호출할 API

운영 세션은 **오전(08:30~10:00)** 과 **오후(14:30~15:30)** 두 개로 나뉘며,
각각 `session` 입력으로 구분한다. dispatch는 **세션 시작 ~10분 전**에 쏴서
러너 기동(checkout + pip) 시간을 확보한다.

```
POST https://api.github.com/repos/asitwas729/auto-trade/actions/workflows/mock-trade.yml/dispatches
Authorization: Bearer <PAT>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28

오전 Body: {"ref":"main","inputs":{"session":"morning"}}
오후 Body: {"ref":"main","inputs":{"session":"afternoon"}}
```

성공 시 **HTTP 204 No Content**(본문 없음)를 반환한다.

curl로 동작 확인 (오전 예시):

```bash
curl -X POST \
  -H "Authorization: Bearer <PAT>" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/asitwas729/auto-trade/actions/workflows/mock-trade.yml/dispatches \
  -d '{"ref":"main","inputs":{"session":"morning"}}'
```

## 3) cron-job.org 설정 (무료, 권장) — 잡 2개

오전/오후 각각 **별도 cronjob 2개**를 만든다. URL·헤더·메서드는 동일하고
**시각과 Request body의 session 값만** 다르다.

| 잡 | 시각(Asia/Seoul) | 요일 | Request body |
|----|------------------|------|--------------|
| 오전 | **08:20** | 월~금 | `{"ref":"main","inputs":{"session":"morning"}}` |
| 오후 | **14:20** | 월~금 | `{"ref":"main","inputs":{"session":"afternoon"}}` |

공통 설정:

1. https://cron-job.org 가입 → **Create cronjob**
2. **URL**: `https://api.github.com/repos/asitwas729/auto-trade/actions/workflows/mock-trade.yml/dispatches`
3. **Schedule**: Timezone **Asia/Seoul**, 위 표의 시각, 요일 월~금
4. **Request method**: **POST**
5. **Request headers**:
   - `Authorization: Bearer <PAT>`
   - `Accept: application/vnd.github+json`
   - `Content-Type: application/json`
6. **Request body**: 위 표 참고 (잡마다 다름)
7. 저장 후 **TEST RUN**으로 204 확인 → Actions 탭에서 워크플로가 떴는지,
   로그의 "Resolve session window"에 의도한 세션이 찍히는지 확인

> 휴장일/주말은 워크플로 안의 `should_run_today.py` 게이트가 걸러주므로,
> 스케줄러는 매 평일 호출해도 된다(주말만 빼면 충분).

## 대안 스케줄러

- **Google Cloud Scheduler** — HTTP 타깃 + 본문/헤더 지정. 무료 한도 내 운영 가능.
- **Cloudflare Workers + Cron Triggers** — Worker에서 `fetch()`로 dispatch 호출.
- **상시 VPS의 crontab** — `45 8 * * 1-5 curl ...` (서버를 직접 들고 있다면 가장 단순).

## 운영 시간 변경

운영 구간은 `MARKET_START_TIME` / `MARKET_END_TIME`(HHMMSS) 환경변수로 제어한다.
워크플로의 "Resolve session window" 스텝이 `session` 입력에 따라 주입한다.

| 세션 | MARKET_START_TIME | MARKET_END_TIME | 주로 도는 전략 |
|------|-------------------|-----------------|----------------|
| morning | 083000 | 100000 | 장전 S5 준비, S1·S5 단타, S9 익일청산 |
| afternoon | 143000 | 153500 | S1 단타, S9 종가베팅(14:30~15:15), 15:25 마감 sweep |

- 시각을 바꾸려면 워크플로의 case 블록 값을 수정한다.
- 전일 운영으로 돌아가려면 `MARKET_START_TIME=090000`, `MARKET_END_TIME=153500`
  단일 세션으로 돌리면 된다. (단, GHA 단일 잡 6h 한도 때문에 전일 운영은
  과거처럼 핸드오프 잡이 다시 필요해질 수 있다.)
