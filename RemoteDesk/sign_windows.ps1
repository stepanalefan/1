# Signs every .exe / .dll / .pyd of the RemoteDesk build so Windows Smart App
# Control and SmartScreen accept it. Files that already carry a valid signature
# (e.g. python3xx.dll from the Python Software Foundation) are left alone.
#
# Configure ONE of these (environment variables, or put the "set" lines in
# sign_config.bat next to build_windows.bat — that file is not committed):
#
#   A) A code-signing certificate installed in Windows (USB token / cloud HSM such
#      as Certum SimplySign, Sectigo, DigiCert ...):
#        set SIGN_CERT_SHA1=<certificate thumbprint>
#
#   B) Azure Trusted Signing (Microsoft's signing service):
#        set SIGN_AZURE_DLIB=C:\path\to\Azure.CodeSigning.Dlib.dll
#        set SIGN_AZURE_METADATA=C:\path\to\metadata.json
#
#   Optional: SIGN_TIMESTAMP_URL (default http://timestamp.digicert.com, or Microsoft's for Azure)
param([Parameter(Mandatory = $true)][string[]]$Folders)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } | Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw "signtool.exe not found. Install the Windows SDK (component 'Windows SDK Signing Tools')."
}

$args = @("sign", "/fd", "SHA256", "/td", "SHA256")
if ($env:SIGN_AZURE_DLIB) {
    $ts = if ($env:SIGN_TIMESTAMP_URL) { $env:SIGN_TIMESTAMP_URL } else { "http://timestamp.acs.microsoft.com" }
    $args += @("/tr", $ts, "/dlib", $env:SIGN_AZURE_DLIB, "/dmdf", $env:SIGN_AZURE_METADATA)
} elseif ($env:SIGN_CERT_SHA1) {
    $ts = if ($env:SIGN_TIMESTAMP_URL) { $env:SIGN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    $args += @("/tr", $ts, "/sha1", $env:SIGN_CERT_SHA1)
} else {
    Write-Host "No signing certificate configured - skipping signing (see sign_windows.ps1)."
    exit 0
}

$signtool = Find-SignTool
$files = foreach ($f in $Folders) {
    Get-ChildItem $f -Recurse -Include *.exe, *.dll, *.pyd -File
}
$todo = @($files | Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -ne "Valid" })
Write-Host "Signing $($todo.Count) of $(@($files).Count) files with $signtool"

# signtool accepts many files per call; sign in batches to keep command lines short
for ($i = 0; $i -lt $todo.Count; $i += 50) {
    $batch = $todo[$i..([Math]::Min($i + 49, $todo.Count - 1))] | ForEach-Object { $_.FullName }
    & $signtool @args @batch
    if ($LASTEXITCODE -ne 0) { throw "signtool failed (exit $LASTEXITCODE)" }
}

$bad = @($files | Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -ne "Valid" })
if ($bad.Count) {
    $bad | ForEach-Object { Write-Host "NOT signed: $($_.FullName)" }
    throw "$($bad.Count) files are not signed"
}
Write-Host "All $(@($files).Count) files carry a valid signature."
