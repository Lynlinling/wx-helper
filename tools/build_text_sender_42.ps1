$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$vcRoot = Join-Path $env:USERPROFILE '.codex/tools/msvc-toolset-14.29/Contents/VC/Tools/MSVC/14.29.30133'
$cl = Join-Path $vcRoot 'bin/Hostx64/x64/cl.exe'
$vcInclude = Join-Path $env:USERPROFILE '.codex/tools/msvc-crt-headers-14.29/Contents/VC/Tools/MSVC/14.29.30133/include'
$vcLib = Join-Path $env:USERPROFILE '.codex/tools/msvc-crt-x64-14.29/Contents/VC/Tools/MSVC/14.29.30133/lib/x64'
$ucrtInclude = Join-Path $env:USERPROFILE '.codex/tools/windows-sdk-ucrt-10.0.28000/c/Include/10.0.28000.0/ucrt'
$sdkLib = Join-Path $repo '.runtime/windows-sdk-cpp-x64-10.0.28000.2705/sdk-libs'
$ucrtLib = Join-Path $sdkLib 'ucrt/x64'
$umLib = Join-Path $sdkLib 'um/x64'
$source = Join-Path $repo 'native/text_sender_42.cpp'
$build = Join-Path $repo 'native/build_text_42'
foreach ($path in @($cl, $vcInclude, $vcLib, $ucrtInclude, (Join-Path $ucrtLib 'libucrt.lib'), (Join-Path $umLib 'kernel32.Lib'), (Join-Path $umLib 'Uuid.Lib'), $source)) {
    if (-not (Test-Path -LiteralPath $path)) { throw ('Required build input missing: ' + $path) }
}
$env:INCLUDE = [string]::Join(';', @($vcInclude, $ucrtInclude))
$env:LIB = [string]::Join(';', @($vcLib, $ucrtLib, $umLib))
New-Item -ItemType Directory -Path $build -Force | Out-Null
$dll = Join-Path $build 'text_sender_42.dll'
$obj = Join-Path $build 'text_sender_42.obj'
& $cl /nologo /std:c++17 /EHsc /W4 /utf-8 /LD /MT /O2 /DNDEBUG $source ('/I' + (Join-Path $repo 'native')) ('/Fo:' + $obj) ('/Fe:' + $dll) /link /MACHINE:X64 /INCREMENTAL:NO /NODEFAULTLIB:OLDNAMES.lib
if ($LASTEXITCODE -ne 0) { throw ('MSVC 4.2 text sender build failed: ' + $LASTEXITCODE) }
if (-not (Test-Path -LiteralPath $dll -PathType Leaf)) { throw 'Expected 4.2 sender DLL was not produced.' }
Write-Output $dll
