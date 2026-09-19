#!/bin/bash
# oom_priority.sh - приоритеты OOM-killer'а на сервере.
#
# На сервере 4 ГБ RAM и несколько Firefox'ов. При нехватке памяти
# жертвой должен становиться посторонний браузер (профиль ai-agent,
# его сторож пересоздаёт браузер сам), а не сбор.
#
# Расставляем oom_score_adj (влияет и на ядро, и на earlyoom):
#   сбор (ff_collect, playwright-драйвер, ms-playwright firefox) -> -200
#   посторонний браузер (firefox --profile .../ai-agent + врапер) -> +500
# Гоняется кроном каждую минуту - новые процессы подхватываются <=60 с.

for p in $(/usr/bin/pgrep -f 'avito-svo/ff_collect|avito-svo/ff_browser_daemon|ms-playwright|playwright/driver' 2>/dev/null); do
    printf '%s\n' -200 > "/proc/$p/oom_score_adj" 2>/dev/null
done

for p in $(/usr/bin/pgrep -f '/root/.mozilla/firefox/ai-agent|persistent_browser_firefox' 2>/dev/null); do
    printf '%s\n' 500 > "/proc/$p/oom_score_adj" 2>/dev/null
done

exit 0
