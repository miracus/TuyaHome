# Збирає шар Lambda для пилососа Dreame: бібліотека Tasshack/dreame-vacuum (MIT) + залежності під Linux
# (Lambda python3.13, x86_64). Результат — infra/build/vacuum_layer/python/…; далі terraform apply.
# Запуск: powershell -ExecutionPolicy Bypass -File D:\repos\TuyaHome\tools\build_vacuum_layer.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$lib = Join-Path $root ".dreame\lib"
$layer = Join-Path $root "infra\build\vacuum_layer"
$out = Join-Path $layer "python"

if (Test-Path $lib) { git -C $lib pull --ff-only } else { git clone --depth 1 https://github.com/Tasshack/dreame-vacuum $lib }
if (Test-Path $layer) { Remove-Item -Recurse -Force $layer }
New-Item -ItemType Directory -Force $out | Out-Null

# лише готові колеса під Linux (нічого не компілюється і не запускається)
$plat = "manylinux2014_x86_64", "manylinux_2_27_x86_64", "manylinux_2_28_x86_64", "manylinux_2_31_x86_64", "manylinux_2_34_x86_64"
$platArgs = $plat | ForEach-Object { "--platform", $_ }
python -m pip install @platArgs --only-binary=:all: --python-version 3.13 --implementation cp `
    --target $out --no-compile --upgrade `
    numpy pycryptodome mini-racer paho-mqtt cryptography pyOpenSSL pillow certifi requests `
    click construct zeroconf attrs pytz appdirs tqdm croniter defusedxml pyyaml
if ($LASTEXITCODE) { throw "pip: залежності не встановились" }
# python-miio — без залежностей (netifaces потребує компілятора і бібліотеці не потрібен)
python -m pip install --no-deps --target $out --no-compile --upgrade python-miio
if ($LASTEXITCODE) { throw "pip: python-miio не встановився" }

Copy-Item -Recurse (Join-Path $lib "custom_components\dreame_vacuum\dreame") (Join-Path $out "dreame")
Copy-Item (Join-Path $lib "LICENSE") (Join-Path $out "dreame\LICENSE")
# кеш Python з локальних запусків (~30 МБ) — Lambda він не потрібен, а ліміт шару 250 МБ
Get-ChildItem $layer -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
(git -C $lib rev-parse --short HEAD) | Out-File (Join-Path $out "dreame\VERSION") -Encoding ascii

$mb = [math]::Round(((Get-ChildItem $layer -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Host "Готово: $layer ($mb МБ без стиснення; ліміт Lambda — 250 МБ)"
