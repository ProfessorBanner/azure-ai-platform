<#
.SYNOPSIS
    AKS MLOps expiry watchdog (Azure Automation, PowerShell 7.4, Az 12.3.0).

.DESCRIPTION
    Runs under the Automation account's system-assigned managed identity on two
    schedules: a one-time schedule at the armed session's expiry and an hourly
    sweep. Both call this runbook. Default Mode is Report: it inventories and
    logs what WOULD be deleted. Mode Execute deletes eligible resources
    idempotently, polls each deletion to completion, re-inventories the session
    and node resource groups, and reports any residual resource as UNRESOLVED.
    It never claims success while resources remain.

    Deletion eligibility requires ALL of:
      1. the job runs in the exact configured subscription;
      2. the target is the exact allowlisted session resource group ID;
      3. the group's tags carry the configured project, the armed session_id,
         lifecycle=disposable and an expiry_utc that has been reached;
      4. a schedule-supplied SessionId (if any) equals the armed session id;
      5. each resource carries lifecycle=disposable and the same session_id.
    Control and retained resources live in other groups and are never listed.

    The runbook is a bounded, best-effort control. Azure Resource Manager can
    accept a delete and still take a long time; a stuck deletion is reported,
    not hidden. This is not a guaranteed hard billing cap.

    G3 status: written and reviewed offline, NOT live-verified. G4 runs it in
    Report mode against an isolated test target before any GPU compute exists.

.PARAMETER Mode
    Report (default) or Execute.
.PARAMETER Trigger
    "expiry" (one-time schedule) or "sweep" (hourly). Informational.
.PARAMETER SessionId
    Session id the one-time schedule was armed for. Must equal the armed id.
#>
param(
    [ValidateSet('Report', 'Execute')]
    [string] $Mode = 'Report',

    [ValidateSet('expiry', 'sweep', 'manual')]
    [string] $Trigger = 'manual',

    [string] $SessionId = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Bounded execution: well under the three-hour fair-share limit.
$MaxDeleteAttempts = 3
$PollIntervalSeconds = 30
$MaxPollMinutes = 40
$JobBudgetMinutes = 75

$startedAt = [DateTime]::UtcNow
$result = [ordered]@{
    schema_version = '1'
    started_utc    = $startedAt.ToString('o')
    mode           = $Mode
    trigger        = $Trigger
    session_id     = ''
    eligible       = $false
    reasons        = @()
    inventory      = @()
    deleted        = @()
    unresolved     = @()
    node_rg_state  = 'unknown'
    outcome        = 'not-eligible'
}

function Write-Result {
    param([hashtable] $Result)
    $Result['finished_utc'] = [DateTime]::UtcNow.ToString('o')
    $json = $Result | ConvertTo-Json -Depth 6
    Write-Output $json
    try {
        $account = Get-AutomationVariable -Name 'aksmlops-results-storage-account'
        $container = Get-AutomationVariable -Name 'aksmlops-results-container'
        $ctx = New-AzStorageContext -StorageAccountName $account -UseConnectedAccount
        $name = ('{0}-{1}-{2}.json' -f $startedAt.ToString('yyyyMMddTHHmmssZ'), $Result['trigger'], $Result['mode'].ToLower())
        $tmp = Join-Path $env:TEMP $name
        Set-Content -Path $tmp -Value $json -Encoding utf8
        Set-AzStorageBlobContent -Context $ctx -Container $container -File $tmp -Blob $name -Force | Out-Null
        Write-Verbose "Result written to $container/$name"
    }
    catch {
        Write-Warning "Result blob not written: $($_.Exception.Message)"
    }
}

try {
    # --- Identity and allowlist --------------------------------------------------
    Disable-AzContextAutosave -Scope Process | Out-Null
    $null = Connect-AzAccount -Identity

    $allowedSubscription = Get-AutomationVariable -Name 'aksmlops-subscription-id'
    $project = Get-AutomationVariable -Name 'aksmlops-project'
    $sessionRgId = Get-AutomationVariable -Name 'aksmlops-session-rg-id'
    $nodeRgId = Get-AutomationVariable -Name 'aksmlops-session-node-rg-id'
    $armedSessionId = Get-AutomationVariable -Name 'aksmlops-armed-session-id'
    $armedExpiryUtc = Get-AutomationVariable -Name 'aksmlops-armed-expiry-utc'

    $null = Set-AzContext -SubscriptionId $allowedSubscription
    $ctxSub = (Get-AzContext).Subscription.Id
    if ($ctxSub -ne $allowedSubscription) {
        $result.reasons += "subscription mismatch: context $ctxSub, allowed $allowedSubscription"
        throw 'refusing to continue outside the configured subscription'
    }

    if ($armedSessionId -eq 'none' -or [string]::IsNullOrWhiteSpace($armedSessionId)) {
        $result.reasons += 'no session armed'
    }
    if ($SessionId -and $SessionId -ne $armedSessionId) {
        $result.reasons += "schedule session id $SessionId does not match armed $armedSessionId"
    }
    $result.session_id = $armedSessionId

    # --- Target group -----------------------------------------------------------
    $expectedRgId = "/subscriptions/$allowedSubscription/resourceGroups/" + ($sessionRgId -split '/')[-1]
    if ($sessionRgId -ne $expectedRgId) {
        $result.reasons += "allowlisted group id is not in the configured subscription: $sessionRgId"
    }

    $rg = Get-AzResourceGroup -Id $sessionRgId -ErrorAction SilentlyContinue
    if (-not $rg) {
        $result.reasons += "session group $sessionRgId not found (already deleted or never created)"
        $result.outcome = 'nothing-to-do'
    }
    else {
        $tags = $rg.Tags
        if (-not $tags) { $tags = @{} }
        if ($tags['project'] -ne $project) { $result.reasons += "group project tag '$($tags['project'])' != '$project'" }
        if ($tags['lifecycle'] -ne 'disposable') { $result.reasons += "group lifecycle tag '$($tags['lifecycle'])' != 'disposable'" }
        if ($tags['session_id'] -ne $armedSessionId) { $result.reasons += "group session_id tag '$($tags['session_id'])' != armed '$armedSessionId'" }
        if ($tags['expiry_utc'] -ne $armedExpiryUtc) { $result.reasons += "group expiry_utc tag '$($tags['expiry_utc'])' != armed '$armedExpiryUtc'" }

        $expiry = $null
        $parsed = [DateTime]::TryParseExact($armedExpiryUtc, "yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal, [ref] $expiry)
        if (-not $parsed) { $result.reasons += "armed expiry '$armedExpiryUtc' is not RFC 3339 UTC" }
        elseif ([DateTime]::UtcNow -lt $expiry) { $result.reasons += "not expired: now $([DateTime]::UtcNow.ToString('o')) < expiry $($expiry.ToString('o'))" }

        # --- Inventory (always, so Report mode is useful) ------------------------------
        $resources = @(Get-AzResource -ResourceGroupName $rg.ResourceGroupName)
        foreach ($r in $resources) {
            $rt = $r.Tags; if (-not $rt) { $rt = @{} }
            $resourceEligible = ($rt['lifecycle'] -eq 'disposable') -and ($rt['session_id'] -eq $armedSessionId)
            $result.inventory += [ordered]@{
                id       = $r.ResourceId
                type     = $r.ResourceType
                eligible = $resourceEligible
            }
        }
        $nodeRg = Get-AzResourceGroup -Id $nodeRgId -ErrorAction SilentlyContinue
        $result.node_rg_state = if ($nodeRg) { 'present' } else { 'absent' }

        $result.eligible = ($result.reasons.Count -eq 0)

        if (-not $result.eligible) {
            $result.outcome = 'not-eligible'
        }
        elseif ($resources.Count -eq 0 -and -not $nodeRg) {
            $result.outcome = 'already-clean'
        }
        elseif ($Mode -eq 'Report') {
            $result.outcome = 'report-only'
            Write-Output "REPORT: $(@($result.inventory | Where-Object { $_.eligible }).Count) eligible resource(s) would be deleted; node group $($result.node_rg_state)."
        }
        else {
            # --- Execute: managed clusters first (their deletion removes the node group) ----
            $ordered = @($resources | Sort-Object { if ($_.ResourceType -eq 'Microsoft.ContainerService/managedClusters') { 0 } else { 1 } })
            foreach ($r in $ordered) {
                $rt = $r.Tags; if (-not $rt) { $rt = @{} }
                if (-not (($rt['lifecycle'] -eq 'disposable') -and ($rt['session_id'] -eq $armedSessionId))) {
                    $result.unresolved += [ordered]@{ id = $r.ResourceId; reason = 'not tagged as this session''s disposable resource; left in place' }
                    continue
                }
                $done = $false
                for ($attempt = 1; $attempt -le $MaxDeleteAttempts -and -not $done; $attempt++) {
                    if (([DateTime]::UtcNow - $startedAt).TotalMinutes -gt $JobBudgetMinutes) { break }
                    try {
                        # Idempotent: a missing resource is success, an accepted delete is NOT.
                        if (-not (Get-AzResource -ResourceId $r.ResourceId -ErrorAction SilentlyContinue)) { $done = $true; break }
                        Remove-AzResource -ResourceId $r.ResourceId -Force -ErrorAction Stop | Out-Null
                        $deadline = [DateTime]::UtcNow.AddMinutes($MaxPollMinutes)
                        while ([DateTime]::UtcNow -lt $deadline) {
                            if (-not (Get-AzResource -ResourceId $r.ResourceId -ErrorAction SilentlyContinue)) { $done = $true; break }
                            Start-Sleep -Seconds $PollIntervalSeconds
                        }
                    }
                    catch {
                        Write-Warning "delete attempt $attempt for $($r.ResourceId) failed: $($_.Exception.Message)"
                    }
                }
                if ($done) { $result.deleted += $r.ResourceId }
                else { $result.unresolved += [ordered]@{ id = $r.ResourceId; reason = 'deletion not confirmed within the job budget' } }
            }

            # --- Re-inventory: the only proof of removal is absence ------------------------
            $remaining = @(Get-AzResource -ResourceGroupName $rg.ResourceGroupName)
            foreach ($r in $remaining) {
                if (-not ($result.unresolved | Where-Object { $_.id -eq $r.ResourceId })) {
                    $result.unresolved += [ordered]@{ id = $r.ResourceId; reason = 'still present after deletion pass' }
                }
            }
            $nodeRgAfter = Get-AzResourceGroup -Id $nodeRgId -ErrorAction SilentlyContinue
            $result.node_rg_state = if ($nodeRgAfter) { 'present' } else { 'absent' }
            if ($nodeRgAfter) {
                $result.unresolved += [ordered]@{ id = $nodeRgId; reason = 'AKS node resource group still present' }
            }
            $result.outcome = if ($result.unresolved.Count -eq 0) { 'clean' } else { 'unresolved' }
        }
    }
}
catch {
    $result.reasons += "error: $($_.Exception.Message)"
    $result.outcome = 'error'
}
finally {
    Write-Result -Result $result
}

if ($result.outcome -in @('unresolved', 'error')) {
    throw "watchdog outcome: $($result.outcome); see result JSON"
}
