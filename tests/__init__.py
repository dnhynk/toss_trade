"""tossmon 테스트 패키지.

이 파일이 **있어야 하는** 이유 (감사 4차 C4):

1. `tests/` 가 정규 패키지가 아니면 네임스페이스 패키지로 해석된다. 네임스페이스 패키지는
   정규 패키지보다 우선순위가 낮아서, 환경의 site-packages 에 **동명의 `tests` 정규 패키지**가
   깔려 있으면 그쪽이 이기고 `from tests import synth` 가 전부 그리로 간다 →
   수집 단계에서 전 스위트 사망(감사 환경에서 실제로 27 errors).

2. 그것과 별개로, `from tests import ...` 는 리포 루트가 sys.path 에 있어야만 동작한다.
   `python -m pytest` 는 cwd 를 sys.path 에 넣어주지만 **`pytest` 콘솔 스크립트는 넣지 않는다.**
   이 파일이 없던 동안 `pytest` 로 실행하면 9개 모듈이 `ModuleNotFoundError: No module named
   'tests'` 로 죽었다 — 즉 "환경 탓"이 아니라 표준 실행 방식 하나가 이미 깨져 있었다.
   `__init__.py` 가 있으면 pytest 가 패키지 루트(=리포 루트)를 sys.path 에 넣으므로 둘 다 된다.

⚠️ 이 파일만 단독으로 추가하면 오히려 스위트가 깨진다. 패키지가 되는 순간 pytest 가
sys.path 에 넣는 것이 `tests/` 가 아니라 리포 루트로 바뀌어서, 형제 모듈을 bare import
(`from test_api_support import ...`) 하던 곳이 전부 실패하기 때문이다. 그래서 이 파일 추가와
형제 import 정리는 **반드시 한 커밋**이어야 한다. 새 테스트를 쓸 때도 형제 모듈은
`from tests.<module> import ...` 로 가져올 것.
"""
