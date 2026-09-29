# -*- mode: python ; coding: utf-8 -*-
"""漫剧工坊 - PyInstaller打包配置"""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# 隐藏导入所有app子模块
hidden_imports = collect_submodules('app')

# 收集所有数据文件
datas = [
    ('app/templates', 'app/templates'),
    ('app/static', 'app/static'),
    ('locales', 'locales'),
    # ⚠️ 2026-09-28 补：工作流模板必须随 exe 分发。PROJECT_WORKFLOWS_DIR 在
    # frozen 模式下 = _MEIPASS/workflows（env_loader 注释：只读资源留 PROJECT_ROOT_DIR），
    # 漏掉这里会让单文件包用户「模板缺失」——克隆/Docker 有 workflows/ 但 exe 没有。
    ('workflows', 'workflows'),
    # ⚠️ 2026-09-29 移除 ('output', 'output')：output/ 是**纯写入目标**
    # （QC_DIR / tasks.db / PROJECTS_DIR 全在 PROJECT_OUTPUT_DIR 下），启动不读它，
    # 打包进 exe 只会让单文件包膨胀 3GB+ 且在 _MEIPASS 里变只读。运行期由
    # main.py 的 output_dir.mkdir(exist_ok=True) 自建（配合 MJSCXT_DATA_DIR 重定向）。
]

# 排除不必要的模块
excludes = [
    'matplotlib', 'scipy', 'pandas', 'jupyter', 
    'tkinter', 'IPython', 'notebook',
]

a = Analysis(
    ['main.py'],
    pathex=['C:\\Users\\liujianghua\\WorkBuddy\\2026-09-09-16-55-22\\漫剧生成系统'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='漫剧工坊',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 调试模式显示控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='x86_64',
    codesign_identity=None,
    entitlements_file=None,
)
