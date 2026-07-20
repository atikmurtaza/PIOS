# Sets your cloud API keys as Windows user environment variables.
# Prompts for input (masked) so the keys never land in PowerShell history.
# Leave a prompt blank to skip that provider.
# To remove a key later:  [Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', $null, 'User')

function Read-Secret($label) {
    $sec = Read-Host "$label (blank = skip)" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { [Runtime.InteropServices.Marshal]::PtrToStringAuto($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host "PIOS cloud API key setup" -ForegroundColor Cyan
Write-Host "Keys are stored in your Windows user environment, not in the PIOS folder.`n"

$anthropic = Read-Secret "Anthropic API key (sk-ant-...)"
if ($anthropic) {
    [Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', $anthropic, 'User')
    $env:ANTHROPIC_API_KEY = $anthropic          # also this window, for a quick test
    Write-Host "  ANTHROPIC_API_KEY set." -ForegroundColor Green
}

$openai = Read-Secret "OpenAI API key (sk-proj-... or sk-...)"
if ($openai) {
    [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', $openai, 'User')
    $env:OPENAI_API_KEY = $openai
    Write-Host "  OPENAI_API_KEY set." -ForegroundColor Green
}

$gemini = Read-Secret "Gemini API key (free tier, aistudio.google.com/apikey)"
if ($gemini) {
    [Environment]::SetEnvironmentVariable('GEMINI_API_KEY', $gemini, 'User')
    $env:GEMINI_API_KEY = $gemini
    Write-Host "  GEMINI_API_KEY set." -ForegroundColor Green
}

Write-Host "`nStored. Verify (should print True):" -ForegroundColor Cyan
Write-Host ("  Anthropic: " + [bool][Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User'))
Write-Host ("  OpenAI:    " + [bool][Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User'))
Write-Host ("  Gemini:    " + [bool][Environment]::GetEnvironmentVariable('GEMINI_API_KEY','User'))
Write-Host "`nNow restart PIOS (close the server window, run run_pios.bat) so it picks these up."
Write-Host "Routing order: Gemini free tier first; Claude/OpenAI only if 'Allow paid APIs' is on in the Privacy tab."
