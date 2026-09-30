@echo off
setlocal
set "PROJECT_DIR=%~dp0"

call "D:\App\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 exit /b %errorlevel%

call "D:\App\miniconda3\condabin\conda.bat" activate "D:\App\python_env\gis312"
if errorlevel 1 exit /b %errorlevel%

cd /d "%PROJECT_DIR%"
python -m nuitka ^
  --standalone ^
  --msvc=latest ^
  --enable-plugin=tk-inter ^
  --windows-console-mode=force ^
  --windows-icon-from-ico=src\logo\logo.ico ^
  --include-data-files=config.json=config.json ^
  --include-data-files=src/logo/logo.ico=src/logo/logo.ico ^
  --include-data-files=src/logo/logo.png=src/logo/logo.png ^
  --include-package=pyogrio ^
  --include-package=rasterio ^
  --include-package=comtypes ^
  --include-package=pywinauto ^
  --report=build\msvc\compilation-report.xml ^
  --output-dir=build\msvc\staging ^
  --output-filename=jinlinOmap.exe ^
  --jobs=4 ^
  --assume-yes-for-downloads ^
  index.py
if errorlevel 1 exit /b %errorlevel%

rem Update binaries without deleting downloaded TIFs, ledgers, or edited config.
robocopy "build\msvc\staging\index.dist" "build\msvc\index.dist" /E /XF config.json /NFL /NDL /NJH /NJS /NP
if errorlevel 8 exit /b %errorlevel%
if not exist "build\msvc\index.dist\config.json" copy /Y "build\msvc\staging\index.dist\config.json" "build\msvc\index.dist\config.json" >nul
exit /b 0
