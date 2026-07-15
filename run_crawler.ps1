# Daily: 워치리스트 수집 → 보강 → 재무 → 텔레그램 브리핑 → 깃헙 배포.
# 작업 스케줄러의 StockBriefCrawler(06:30)가 run_crawler.bat을 거쳐 이걸 부른다.
#
# 이 파이프라인은 2026-07-14부터 조용히 죽어 있었다. 작업 스케줄러는 매일 결과를
# 남겼지만 실제로는 아무것도 안 했다. 원인 (2026-07-16 규명):
#  ① `python x.py`처럼 이름으로 부르면 스케줄 실행에서 해석 실패한다. 파이썬이
#     레지스트리 PATH(사용자·시스템)에 등록돼 있지 않아 터미널에서만 잡힌다.
#  ② 실패를 삼켰다. 5단계가 앞 단계 성공 여부와 무관하게 그냥 다 돌고 exit 0으로 끝났다.
#     (crawler.py가 죽어도 publish.py가 옛 데이터를 그대로 재배포했다)
#
# 로직을 .bat에 두지 않는 이유: cmd에서 `%date%`가 `07-16(목)`이라 괄호가 블록을
# 조기 종료시켜 구문 오류가 난다. 한글도 bat에선 파싱을 깨뜨린다(podium-blog 교훈).
$env:PYTHONIOENCODING = 'utf-8'
Set-Location C:\ohai\stock-brief

$RunLog = 'C:\ohai\stock-brief\run_crawler.log'
function Note($msg) {
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg
    Write-Output $line
    Add-Content -Path $RunLog -Value $line -Encoding utf8
}
function Resolve-Python {
    $c = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($c) { return $c }
    $c = & py -3 -c "import sys; print(sys.executable)" 2>$null   # py.exe는 C:\WINDOWS에 있어 항상 잡힌다
    if ($LASTEXITCODE -eq 0 -and $c -and (Test-Path $c.Trim())) { return $c.Trim() }
    return $null
}

$PY = Resolve-Python
if (-not $PY) {
    Note "FAIL 파이썬을 찾을 수 없다 (PATH·py 런처 모두 실패) — 알림도 못 보낸다"
    exit 9
}

Note "시작 ($PY)"
foreach ($step in 'crawler.py', 'enrich.py', 'financials.py', 'notify_telegram.py', 'publish.py') {
    & $PY $step
    if ($LASTEXITCODE -ne 0) {
        Note "FAIL $step 종료코드 $LASTEXITCODE — 이후 단계 중단"
        try { & $PY C:\ohai\telegram-notify\notify.py "스톡브리프 실패: $step (종료코드 $LASTEXITCODE)" | Out-Null } catch { }
        exit 1
    }
}
Note "완료"
