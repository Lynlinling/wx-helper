param(
    [ValidateRange(1, 1000)]
    [int]$Limit = 2,
    [ValidateRange(128, 4096)]
    [int]$MaxTokens = 1536,
    [string]$Output = ".runtime\lyn-style-eval.jsonl",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$dataset = Join-Path $projectRoot "..\..\research\lyn-dialogue-eval.jsonl"
$profile = Join-Path $projectRoot "..\..\research\lyn-style-profile.draft.json"
$config = Join-Path $projectRoot "llm_config.json"
$output = if ([System.IO.Path]::IsPathRooted($Output)) { $Output } else { Join-Path $projectRoot $Output }
$generator = Join-Path $PSScriptRoot "style_eval_generate.py"

if (-not (Test-Path -LiteralPath $config)) {
    throw "llm_config.json not found: $config"
}
if ((Test-Path -LiteralPath $output) -and ((Get-Item -LiteralPath $output).Length -gt 0) -and -not $Force) {
    throw "Output already exists: $output . Use -Output for a new file or -Force to replace it."
}

Write-Host "Generating $Limit A/B items with max_tokens=$MaxTokens."
python $generator $dataset $config $profile $output --limit $Limit --max-tokens $MaxTokens
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Write-Host "Done: $output"
Write-Host "Fill preferred=a/b/tie/skip, then run:"
Write-Host "python tools\style_eval.py score .runtime\lyn-style-eval.jsonl"
