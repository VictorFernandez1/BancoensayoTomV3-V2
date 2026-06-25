# -*- mode: python ; coding: utf-8 -*-

#   PARA INCLUIR ÚNICAMENTE LAS LIBRERÍAS DE UN VENV, ASEGURARSE DE QUE DICHO VENV TIENE PYINSTALLER
#   LUEGO, DESDE DENTRO DEL VENV, CORRER .\integratedvenv\Scripts\python.exe -m PyInstaller --clean main.spec
a = Analysis(
    ['backend\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('frontend', 'frontend')],
    hiddenimports=[
        'aiofiles', 'aiofiles._core', 'aiofiles.threadpool',
        'fastapi', 'uvicorn', 'uvicorn.lifespan.on',
        'starlette', 'starlette.middleware.cors',
        'websockets', 'pyserial', 'bleak', 'bleak.backends.winrt',
        'pandas', 'cv2', 'serial'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # important for onedir
    name='BancoEnsayoTOMV3',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BancoEnsayoTOMV3'
)
