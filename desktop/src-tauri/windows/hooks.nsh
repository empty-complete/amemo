; Хуки установщика NSIS (bundle.windows.nsis.installerHooks в tauri.conf.json; шаблон — tauri-bundler, installer.nsi).
; Установка — NSIS_HOOK_PREINSTALL ниже; удаление — NSIS_HOOK_PREUNINSTALL и NSIS_HOOK_POSTUNINSTALL.
;
; Удаление (спецификация приложения для Windows, п. 13). Обновление (updater, флаг /UPDATE) удалитель не запускает, а
; если запускает (переустановка) с /UPDATE — $UpdateMode = 1, и ничего из amemo не убирается.
; 1. Остановить фон, иначе процессы службы и PostgreSQL держали бы файлы комплекта: `amemo.exe --quit` передаёт
;    команду работающему приложению (single-instance) и ждёт, пока оно остановит службу и PostgreSQL; приложение не
;    запущено — команда просто завершается. Если его закрыли без этого (снятая задача), PostgreSQL останавливает
;    `bin\amemo.exe pg stop`, а оставшиеся процессы Python комплекта завершаются.
; 2. Убрать подключения агентов (`amemo uninstall claude-code`, `codex`) — пока запускатель и комплект на месте.
;    Плагин в аккаунте claude.ai убирает человек (Customize → Plugins).
; 3. После удаления файлов приложения — комплект, загрузки, журналы и state.json. Ключ автозапуска HKCU\…\Run удаляет
;    сам шаблон. Папка данных (база, хранилище копий, бэкапы) и amemo.toml остаются: их удаляет человек.
; Установка поверх работающего amemo (обновление). Запускатель bin\amemo.exe держат открытым серверы MCP, которые
; запустил агент (`amemo.exe mcp` — дочерний процесс Claude или Codex): пока они работают, файл нельзя перезаписать,
; и установщик останавливался на «Error opening file for writing». Работающий exe в Windows можно переименовать:
; прежний запускатель уходит в amemo.exe.oldN, на его место встаёт новый. Сервер MCP дорабатывает на прежнем
; до перезапуска агента; его копию убирает следующая установка или удаление — когда её уже никто не держит.
!macro NSIS_HOOK_PREINSTALL
  Delete "$INSTDIR\bin\amemo.exe.old*"
  ${If} ${FileExists} "$INSTDIR\bin\amemo.exe"
    Push $0
    StrCpy $0 0
    ${Do}
      IntOp $0 $0 + 1
    ${LoopWhile} ${FileExists} "$INSTDIR\bin\amemo.exe.old$0"
    Rename "$INSTDIR\bin\amemo.exe" "$INSTDIR\bin\amemo.exe.old$0"
    Pop $0
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ${If} ${FileExists} "$INSTDIR\${MAINBINARYNAME}.exe"
    ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --quit'
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\bin\amemo.exe"
    nsExec::Exec '"$INSTDIR\bin\amemo.exe" pg stop'
    Pop $0
    ${If} $UpdateMode <> 1
      nsExec::Exec '"$INSTDIR\bin\amemo.exe" uninstall claude-code'
      Pop $0
      nsExec::Exec '"$INSTDIR\bin\amemo.exe" uninstall codex'
      Pop $0
    ${EndIf}
  ${EndIf}
  nsExec::Exec `powershell -NoProfile -NonInteractive -Command "Get-Process python -ErrorAction SilentlyContinue | Where-Object Path -like '$INSTDIR\runtime\*' | Stop-Process -Force"`
  Pop $0
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    RMDir /r "$INSTDIR\runtime"
    RMDir /r "$INSTDIR\downloads"
    RMDir /r "$INSTDIR\logs"
    Delete "$INSTDIR\bin\amemo.exe.old*"
    RMDir "$INSTDIR\bin"
    Delete "$INSTDIR\state.json"
    RMDir "$INSTDIR"
  ${EndIf}
!macroend
