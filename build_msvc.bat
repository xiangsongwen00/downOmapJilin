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
  --include-data-files=config.json=config.json ^
  --include-package=pyogrio ^
  --include-package=rasterio ^
  --include-package=comtypes ^
  --include-package=pywinauto ^
  --report=build\msvc\compilation-report.xml ^
  --output-dir=build\msvc ^
  --output-filename=OviExporter.exe ^
  --jobs=4 ^
  --assume-yes-for-downloads ^
  index.py
exit /b %errorlevel%
