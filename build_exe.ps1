# 在 Windows 上把 multisign.py 打成单文件 exe（需在 Windows 上执行，Linux 无法交叉编译 Windows exe）
# 用法：powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) { python -m venv .venv }
. .venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt pyinstaller

# 字体随包一起带上，exe 才能写中文
$font = "C:\Windows\Fonts\simsun.ttc"
if (-not (Test-Path $font)) { $font = "C:\Windows\Fonts\msyh.ttc" }

pyinstaller --noconfirm --onefile --name multisign `
  --collect-all pyhanko --collect-all pyhanko_certvalidator `
  --collect-all certvalidator --collect-all uharfbuzz --collect-all fontTools `
  --collect-all pikepdf --collect-all pymupdf `
  --add-data "$font;fonts" `
  --hidden-import tzlocal --hidden-import asn1crypto `
  multisign.py

Write-Host "`n生成完成: dist\multisign.exe"
Write-Host "用法:  .\dist\multisign.exe -c job.json --preview preview.png"
Write-Host "       .\dist\multisign.exe --verify 已签名.pdf"
