; Agent Collab 的 NSIS 安装器钩子（tauri.conf.json 的 bundle.windows.nsis.installerHooks），见 docs/desktop.md“构建”。
; instance.nsh 由 build.rs 按构建的实例名生成，定义 AC_PREFIX_NAME（本机部署的安装前缀目录名）与 AC_INSTANCE（实例名）。
!include "${__FILEDIR__}\instance.nsh"

; 指明是哪一份本机部署：与桌面端“卸载本机后台”传给安装器的参数相同（实例名为空时由安装器取缺省值）。
!if "${AC_INSTANCE}" == ""
  !define AC_LOCATION ""
!else
  !define AC_LOCATION ' --prefix "$R8" --state-dir "$R6" --instance ${AC_INSTANCE}'
!endif

; 覆盖安装与更新：复制文件之前清空受管的随包载荷（运行时、本机助手、后台发行包、许可材料与载荷清单），
; 上一版删除或改名的模块因此不会留在执行路径上（ADR 0004 第 7 条）。只删这几项：用户数据、凭据、
; 项目与本机部署都不在应用目录里。中途被打断时载荷不完整，程序启动助手前的核对会拒绝并提示重新安装。
!macro NSIS_HOOK_PREINSTALL
  RMDir /r "$INSTDIR\resources\app"
  RMDir /r "$INSTDIR\resources\runtime"
  RMDir /r "$INSTDIR\resources\release"
  RMDir /r "$INSTDIR\resources\licenses"
  Delete "$INSTDIR\resources\payload.json"
!macroend

; 记下安装时的界面语言：卸载程序（MUI_UNGETLANGUAGE）按它显示同一种语言。不显示语言选择框时模板不写这个值，
; 卸载程序就退回第一种语言（英文），中文系统上安装是中文、卸载却是英文（M3 收尾时在虚拟机里发现）。卸载时模板会删掉它。
!macro NSIS_HOOK_POSTINSTALL
  WriteRegStr HKCU "${MANUPRODUCTKEY}" "Installer Language" $LANGUAGE
!macroend

; 卸载桌面端：本机后台（controller、节点的服务与程序文件）由安装前缀里的安装器管理，不随应用目录删除。
; - 静默卸载与选择“否”：保留后台，服务继续运行；在前缀里写 desktop-uninstalled.txt，说明保留了哪个后台、怎样继续管理。
;   更新（/UPDATE）不经过这里改动后台。
; - 选择“是”：用随包的运行时与本机助手运行 install --uninstall --purge-code，与桌面端“卸载本机后台”相同：
;   停用并移除服务、启动项与程序文件（versions、runtime），状态目录（数据库、节点身份、备份与日志）保留。
;   没有完全清除时如实说明，可以取消这次卸载、保留桌面端；安装器的卸载可以重复运行，再次运行卸载程序会接着清除。
; 有 launcher.py 就是有受管的本机部署；只剩 versions 或 runtime 时是上一次没清除完的程序文件，同样询问。
!macro NSIS_HOOK_PREUNINSTALL
  StrCpy $R8 "$PROFILE\.local\share\${AC_PREFIX_NAME}"
  StrCpy $R6 "$PROFILE\.local\state\${AC_PREFIX_NAME}"
  IfFileExists "$R8\launcher.py" ac_found
  IfFileExists "$R8\versions\*.*" ac_found
  IfFileExists "$R8\runtime\*.*" ac_found ac_no_backend
  ac_found:
  ${If} $UpdateMode = 1
    Goto ac_no_backend
  ${EndIf}
  IfSilent ac_keep_backend
  ${If} $LANGUAGE = 2052
    MessageBox MB_YESNO|MB_ICONQUESTION "这台机器上有 Agent Collab 的本机后台（controller 或节点的服务与程序文件，在 $R8）。它不随桌面端卸载，服务会继续运行。$\r$\n$\r$\n要同时卸载本机后台吗？会停用并移除它的服务、启动项与程序文件；数据库、节点身份、备份与日志保留在状态目录 $R6。$\r$\n$\r$\n选“否”保留本机后台，说明写在 $R8\desktop-uninstalled.txt。" IDNO ac_keep_backend
  ${Else}
    MessageBox MB_YESNO|MB_ICONQUESTION "This machine has an Agent Collab local backend (controller or node services and program files in $R8). It is not removed with the desktop app and its services keep running.$\r$\n$\r$\nAlso uninstall the local backend? Its services, startup entries and program files are removed; the database, node identity, backups and logs are kept in $R6.$\r$\n$\r$\nChoose No to keep it; a note is written to $R8\desktop-uninstalled.txt." IDNO ac_keep_backend
  ${EndIf}
  DetailPrint "install --uninstall --purge-code: $R8"
  StrCpy $R7 "$INSTDIR\resources\runtime\python\python.exe"
  IfFileExists "$R7" +2
    StrCpy $R7 "$INSTDIR\resources\runtime\python.exe"
  ${If} ${FileExists} "$R7"
  ${AndIf} ${FileExists} "$INSTDIR\resources\app\helper.py"
    nsExec::ExecToLog '"$R7" -B -s -E -X utf8 "$INSTDIR\resources\app\helper.py" install --uninstall --purge-code${AC_LOCATION}'
  ${Else}
    ; 随包运行时不完整（安装损坏）：退回前缀里的启动器。它运行的那份运行时不能删除自己，会留下 runtime，下面按没有清除完处理。
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$R8\bin\agent-collab.ps1" install --uninstall --purge-code${AC_LOCATION}'
  ${EndIf}
  Pop $R9
  ${If} $R9 == 0
  ${AndIfNot} ${FileExists} "$R8\launcher.py"
  ${AndIfNot} ${FileExists} "$R8\versions\*.*"
  ${AndIfNot} ${FileExists} "$R8\runtime\*.*"
    DetailPrint "OK: $R6"
    ${If} $LANGUAGE = 2052
      MessageBox MB_OK|MB_ICONINFORMATION "已卸载本机后台：服务、启动项与程序文件已移除。$\r$\n状态目录（数据库、节点身份、备份与日志）保留在 $R6；要彻底删除，自行删除这个目录。"
    ${Else}
      MessageBox MB_OK|MB_ICONINFORMATION "The local backend was uninstalled: services, startup entries and program files were removed.$\r$\nThe state directory (database, node identity, backups and logs) is kept in $R6; delete it yourself to remove everything."
    ${EndIf}
    Goto ac_no_backend
  ${EndIf}
  DetailPrint "incomplete: $R9"
  ${If} $LANGUAGE = 2052
    MessageBox MB_YESNO|MB_ICONEXCLAMATION "本机后台没有完全清除（安装器结果：$R9；“显示细节”里有它的输出）。服务、启动项或程序文件可能还有残留；残留的程序文件不会自行运行。$\r$\n$\r$\n选“否”取消这次卸载、保留桌面端：关闭占用这些文件的程序后再次运行卸载程序，会接着清除。$\r$\n$\r$\n选“是”照常卸载桌面端：之后在 PowerShell 里运行 & $\"$R8\bin\agent-collab.ps1$\" install --uninstall --purge-code --prefix $\"$R8$\"（启动器还在时），或手动删除 $R8\versions 与 $R8\runtime。" IDYES ac_no_backend
  ${Else}
    MessageBox MB_YESNO|MB_ICONEXCLAMATION "The local backend was not fully removed (installer result: $R9; see Show details for its output). Services, startup entries or program files may remain; leftover program files do not run by themselves.$\r$\n$\r$\nChoose No to cancel this uninstall and keep the desktop app: close the programs using these files and run the uninstaller again to finish.$\r$\n$\r$\nChoose Yes to uninstall the desktop app anyway: later run & $\"$R8\bin\agent-collab.ps1$\" install --uninstall --purge-code --prefix $\"$R8$\" (if the launcher is still there), or delete $R8\versions and $R8\runtime by hand." IDYES ac_no_backend
  ${EndIf}
  Abort
  ac_keep_backend:
    ClearErrors
    FileOpen $R7 "$R8\desktop-uninstalled.txt" w
    ${IfNot} ${Errors}
      FileWriteWord $R7 0xFEFF
      FileWriteUTF16LE $R7 "Agent Collab 桌面端卸载时保留了这台机器上的本机后台（controller 或节点的服务与程序文件）；已安装的服务继续运行。$\r$\n"
      FileWriteUTF16LE $R7 "实例：${AC_INSTANCE}（为空即缺省实例）$\r$\n安装前缀（程序）：$R8$\r$\n状态目录（数据库、节点身份、备份与日志）：$R6$\r$\n$\r$\n"
      FileWriteUTF16LE $R7 "继续管理：重新安装 Agent Collab 桌面端，在“连接”页的“本机服务”里查看、升级或卸载；或在 PowerShell 里运行$\r$\n"
      FileWriteUTF16LE $R7 "  & $\"$R8\bin\agent-collab.ps1$\" service status --prefix $\"$R8$\"$\r$\n"
      FileWriteUTF16LE $R7 "  & $\"$R8\bin\agent-collab.ps1$\" install --uninstall --purge-code --prefix $\"$R8$\"$\r$\n"
      FileWriteUTF16LE $R7 "后一条停用并移除服务、启动项与程序文件，状态目录保留；它自己正在使用的运行时目录 $R8\runtime 会留下，之后可以直接删除。$\r$\n$\r$\n"
      FileWriteUTF16LE $R7 "The Agent Collab desktop app was uninstalled and kept the local backend on this machine (controller or node services and program files); installed services keep running.$\r$\n"
      FileWriteUTF16LE $R7 "Instance: ${AC_INSTANCE} (empty means the default instance). Install prefix: $R8. State directory: $R6.$\r$\n"
      FileWriteUTF16LE $R7 "To manage it, reinstall the desktop app (Connections > Local service) or run the two PowerShell commands above; the second removes services, startup entries and program files, keeps the state directory, and leaves the runtime directory it runs from ($R8\runtime) for you to delete.$\r$\n"
      FileClose $R7
    ${EndIf}
    DetailPrint "kept: $R8\desktop-uninstalled.txt"
  ac_no_backend:
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
!macroend
