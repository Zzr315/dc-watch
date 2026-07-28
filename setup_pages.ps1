<#
One-time GitHub Pages setup. Run this AFTER creating an empty public repo in
the browser (the gh CLI is not installed on this machine, so the repo itself
has to be made by hand).

    powershell -ExecutionPolicy Bypass -File setup_pages.ps1 -User <你的GitHub用户名>
    powershell -ExecutionPolicy Bypass -File setup_pages.ps1 -User bopu -Repo dc-watch

What it does, in order:
  1. sets a repo-local git identity (email uses GitHub's noreply form so your
     real address never lands in a public commit log)
  2. adds the remote
  3. runs the publish safety gate, then commits and pushes

It deliberately does NOT create the repo or flip the Pages switch — both need a
browser. The script stops with clear instructions if the remote is not reachable.
#>
param(
    [Parameter(Mandatory = $true)][string]$User,
    [string]$Repo = "dc-watch",
    [string]$Name = ""
)

# Native exes (git, python) write progress to stderr; under "Stop" that is
# treated as terminating. Check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if ([string]::IsNullOrWhiteSpace($Name)) { $Name = $User }
$remote = "https://github.com/$User/$Repo.git"

Write-Host "仓库   : $remote"
Write-Host "提交者 : $Name <$User@users.noreply.github.com>"
Write-Host ""

# --- 1. identity, scoped to this repo only -------------------------------
git config user.name  $Name
git config user.email "$User@users.noreply.github.com"

# --- 2. remote ----------------------------------------------------------
# `git remote` just lists names and never writes to stderr, unlike
# `git remote get-url origin` on a missing remote
$remotes = @(git remote)
if ($remotes -contains "origin") {
    $existing = (git remote get-url origin)
    if ($existing -ne $remote) {
        Write-Host "origin 已存在且不同，改为 $remote"
        git remote set-url origin $remote
    }
} else {
    git remote add origin $remote
}

# --- 3. is the repo actually there? -------------------------------------
Write-Host "检查远端可达（首次会弹浏览器登录）..."
git ls-remote origin | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "远端不可达。多半是仓库还没建，或者还没登录。" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  1. 打开 https://github.com/new"
    Write-Host "     Repository name : $Repo"
    Write-Host "     选 Public（Pages 免费版只支持公开仓库）"
    Write-Host "     不要勾 Add a README / .gitignore / license —— 要完全空的仓库"
    Write-Host "  2. 建好后重新运行本脚本，会弹出浏览器让你登录 GitHub"
    Write-Host ""
    exit 1
}

# --- 4. safety gate, then commit + push ---------------------------------
Write-Host ""
Write-Host "运行发布安全闸..."
python publish.py --dry-run
if ($LASTEXITCODE -ne 0) {
    Write-Error "安全闸拦下了内容，未提交。按上面提示修 .gitignore 后重试。"
    exit 2
}

git add --all
if ($LASTEXITCODE -ne 0) { Write-Error "git add 失败"; exit 2 }
$n = @(git diff --cached --name-only).Count
if ($n -eq 0) {
    Write-Host "没有变更需要提交。"
} else {
    git commit -m "Data Center Watch: initial publish" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "git commit 失败"; exit 2 }
    Write-Host "已提交 $n 个文件"
}

Write-Host "推送到 origin/main（首次会弹浏览器登录）..."
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Error "推送失败。若是认证问题，装 Git Credential Manager 或改用 SSH。"
    exit 3
}

Write-Host ""
Write-Host "推送成功。还剩最后一步（必须在浏览器里做）：" -ForegroundColor Green
Write-Host ""
Write-Host "  打开 https://github.com/$User/$Repo/settings/pages"
Write-Host "    Source : Deploy from a branch"
Write-Host "    Branch : main   目录选 /docs   然后 Save"
Write-Host ""
Write-Host "一两分钟后你的链接就是："
Write-Host "  https://$User.github.io/$Repo/" -ForegroundColor Cyan
Write-Host ""
Write-Host "之后每天 08:00 / 20:00 自动更新，需要把发布打开："
Write-Host "  powershell -ExecutionPolicy Bypass -File setup_task.ps1 -Publish"
