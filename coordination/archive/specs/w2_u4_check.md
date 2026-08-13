# W2 — U-4 패턴 점검: 예외 체인에 파일 원문이 남는 파서 (짧은 점검 태스크)

너는 W2(`tossmon/store/**`, `tossmon/universe/**`, `tests/test_store*.py`,
`tests/test_universe*.py` 소유자)다. 직전 태스크(심볼 문자셋)는 머지 완료.
이번 건은 W1이 자기 소유 경로에서 발견·수정한 **U-4 유출 패턴**이 네 소유 경로에도
있는지 전수 점검하는 짧은 태스크다.

**라이브 호출 금지**, `api_keys` 읽기 금지, W5 DB 접근 금지 (W5가 라이브 리스로 재수집 중).

## 시작 절차

`git rebase main` 먼저 (main = `6def9b5`, 749 passed·1 skipped).

## 배경 — U-4 패턴이란

- `json.JSONDecodeError`는 `str(e)`에 위치만 담지만, **`e.doc`에 파싱하려던 문자열 전문**을
  들고 다닌다. 비슷하게 다른 파서 예외도 원문 조각을 args에 실을 수 있다.
- **`raise X from None`은 `__context__`를 지우지 않는다** — traceback **표시**만 억제된다.
  예외를 깊게 덤프하는 로거·에러리포터·디버거가 체인을 따라가면 원문이 그대로 샌다.
- 고치는 법: except 블록 **안에서** raise하지 말고, 플래그만 세워 블록을 빠져나온 뒤
  **밖에서** raise한다 → 활성 예외가 없으므로 `__context__`가 아예 None이 된다.
- 구현·테스트 예시(main에 있음): `tossmon/api/tokens.py`의 `_read_keys`,
  `tests/test_api_audit_regressions.py`의 `test_malformed_json_api_keys_never_leaks_content`.

## 작업

1. 네 소유 경로 전체에서 **파일/외부 입력을 파싱하고 예외를 감싸는 지점**을 나열하라
   (유니버스 시드 파일 로더 포함 — json/csv/yaml/텍스트 파싱 전부).
2. 각 지점을 점검하라 (라이브 불필요, 지점당 ~1분):
   1. 센티널 문자열(`SENTINEL_VALUE`)을 심은 **깨진** 입력 파일을 만든다.
   2. 로더를 호출해 예외를 잡는다.
   3. 예외 체인을 깊이 순회하며 센티널을 찾는다 — 4곳 전부:
      `str(exc)`, `traceback.format_exc()`, `exc.doc`/`exc.msg`/`exc.args`,
      `exc.__cause__`/`exc.__context__`(재귀).
   4. 통과 기준: `__context__`·`__cause__` 둘 다 None이고 위 어디에도 원문 없음.
3. **판정 2축**: (i) 원문이 체인에 남는가, (ii) 그 원문이 민감할 수 있는가 —
   시드 파일은 공개 심볼 목록이라 내용 자체는 민감하지 않지만, **운영자가 경로 설정을
   바꿔 다른 파일을 가리키게 될 수 있는 로더라면 민감 취급**하라(경로는 설정 가능한 값이다).
4. 원문이 남는 지점은 W1 방식(블록 밖 raise)으로 체인을 끊고, **센티널 회귀 테스트**를 붙여라.
   민감성이 없어 수정하지 않기로 판단한 지점은 **근거와 함께 (c)에 보고**하라
   (수정 자체는 싸니 웬만하면 고치는 쪽을 권장).
5. 전체 `pytest` 통과 확인.

## 불변 규칙

1. 소유 경로 밖 수정 금지. 완료 시 `git diff --name-only $(git merge-base main HEAD)..HEAD`로 증명.
2. `main` 직접 커밋·머지 금지. `w2-universe-store`에만 커밋. 보고 전 `git log` 확인.
3. 라이브 호출·`api_keys` 읽기 금지, W5 DB 접근 금지.
4. `worker_done` 정확히 1회, 보고 (a)~(e), 이후 idle.
5. 콘솔에 비ASCII 출력 금지(cp949). 보고문 셸 인자에 백틱 넣지 마라(명령 치환 사고 전례).
