@echo off
rem tossmon collector 기동 런처 — 소유: W5.
rem 작업 스케줄러(tossmon-collector-oneshot)가 이 파일을 실행한다. docs/11 §11-1 후속 (ii)
rem 해소: 기동 env 를 Temp 임시파일/인라인 /TR 이 아니라 리포 안에 고정해 재현성을 확보한다.
rem TOSS_BASE_URL 값은 비밀이 아니다(기동 로그에 상시 노출되는 값). 토큰/키는 여기 두지 않는다.
set "TOSS_BASE_URL=https://openapi.tossinvest.com"
set "TOSS_LIVE=1"
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m ops.supervisor --config ops\ops_config.yaml >> data\supervisor.stdout.log 2>&1
