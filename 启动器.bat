@echo off
setlocal
title YuE2 Studio 1.0.0
cd /d %~dp0
set PYTHONNOUSERSITE=1
set PYTHONUTF8=1
set RUNDIR=%~dp0
set RUNDIR=%RUNDIR:~0,-1%
set VIRTUAL_ENV=%RUNDIR%
set FFMPEG_PATH=%VIRTUAL_ENV%\env\ffmpeg\bin
set PATH=%SystemRoot%\system32;%SystemRoot%
set PATH=%FFMPEG_PATH%;%VIRTUAL_ENV%\env;%VIRTUAL_ENV%\env\Scripts;%PATH%

echo -----------------------------------------------------------------------------
echo "                            _ooOoo_  "
echo "                           o8888888o "
echo "                           88\ . \88 "
echo "                           (| -_- |) "
echo "                           O\\  =  /O "
echo "                        ____/\`---'\\____ "
echo "                      .'  \\\\|     |//  \`. "
echo "                     /  \\\\|||  :  |||//  \\ "
echo "                    /  _||||| -:- |||||-  \\ "
echo "                    |   | \\\\\\  -  /// |   | "
echo "                    | \\_|  ''\\---/''  |   | "
echo "                    \\  .-\\__  \`-\`  ___/-. / "
echo "                  ___\`. .'  /--.--\\  \`. . __ "
echo "               .\\ '<  \`.___\\_<|>_/___.'  >'\. "
echo "              | | :  \`- \\\`.;\`\\ _ /\`;.\`/ - \` : | | "
echo "              \\  \\ \`-.   \\_ __\\ /__ _/   .-\` /  / "
echo "         ======\`-.____\`-.___\\_____/___.-\`____.-'====== "
echo "                            \`=---='  "
echo "         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ "
echo -----------------------------------------------------------------------------


"%VIRTUAL_ENV%\env\python.exe" -m app.main
pause
