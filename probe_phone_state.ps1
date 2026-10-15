# Read-only Windows telephony state probe. Run with Windows PowerShell 5.1.
# Does not dial, answer, hang up, register an app, or change audio routing.
# Phone numbers, contact names, line IDs and call IDs never leave this process.
# Official API: https://learn.microsoft.com/en-us/uwp/api/windows.applicationmodel.calls.phonecallstatus
param(
    [ValidateRange(100, 5000)][int]$IntervalMs = 250,
    [switch]$Once,
    [switch]$Diagnostic
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Emit-State([hashtable]$State) {
    $State.source = 'windows-phone-call-api'
    $State.observedAt = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    [Console]::WriteLine(($State | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
}

function Get-NonterminalCalls($Calls) {
    # Windows may retain Ended/Lost entries in AllActivePhoneCalls after a new
    # call starts. They are history, not a second ongoing call.
    foreach ($phoneCall in $Calls) {
        if ($phoneCall.Status.ToString() -notin @('Ended', 'Lost')) { $phoneCall }
    }
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [void][Windows.ApplicationModel.Calls.PhoneCallStore,Windows.ApplicationModel.Calls,ContentType=WindowsRuntime]
    [void][Windows.ApplicationModel.Calls.PhoneLine,Windows.ApplicationModel.Calls,ContentType=WindowsRuntime]
    [void][Windows.ApplicationModel.Calls.PhoneCallsResult,Windows.ApplicationModel.Calls,ContentType=WindowsRuntime]
    $script:asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        } | Select-Object -First 1
    if (-not $script:asTask) { throw 'WinRT task conversion unavailable' }
} catch {
    Emit-State @{available=$false; active=$false; callPhase='unavailable'; callDirection='unknown'; callCount=0; callSequence=0; errorCode='api-unavailable'}
    exit 1
}

function Await-WinRT($Operation, [Type]$ResultType) {
    $task = $script:asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait(1500)) {
        try { $Operation.Cancel() } catch {}
        throw 'WinRT query timeout'
    }
    return $task.Result
}

$store = $null
$lastCallId = $null
$lastCallDirection = 'unknown'
$callSequence = 0
do {
    $started = [Diagnostics.Stopwatch]::StartNew()
    $probeStage = 'store'
    $probeReason = 'query-error'
    try {
        if (-not $store) {
            $store = Await-WinRT ([Windows.ApplicationModel.Calls.PhoneCallManager]::RequestStoreAsync()) ([Windows.ApplicationModel.Calls.PhoneCallStore])
            if (-not $store) { throw 'Phone call store unavailable' }
        }
        $probeStage = 'default-line'
        $lineId = Await-WinRT ($store.GetDefaultLineAsync()) ([Guid])
        if ($lineId -eq [Guid]::Empty) { $probeReason = 'default-line-unavailable'; throw 'Default line unavailable' }
        $probeStage = 'line'
        $line = Await-WinRT ([Windows.ApplicationModel.Calls.PhoneLine]::FromIdAsync($lineId)) ([Windows.ApplicationModel.Calls.PhoneLine])
        if (-not $line -or $line.Transport.ToString() -ne 'Bluetooth') {
            throw 'Default line is not Bluetooth'
        }
        $probeStage = 'calls'
        $result = Await-WinRT ($line.GetAllActivePhoneCallsAsync()) ([Windows.ApplicationModel.Calls.PhoneCallsResult])
        if ($result.OperationStatus.ToString() -ne 'Succeeded') { $probeReason = 'calls-' + $result.OperationStatus.ToString(); throw 'Phone call query failed' }
        $calls = @(Get-NonterminalCalls $result.AllActivePhoneCalls)
        $state = @{available=$true; active=$false; callPhase='idle'; callDirection='unknown'; callCount=$calls.Count; callSequence=$callSequence; errorCode=$null}
        if ($calls.Count -eq 0) {
            $lastCallId = $null
            $lastCallDirection = 'unknown'
        } elseif ($calls.Count -eq 1) {
            $call = $calls[0]
            # Keep the identifier in memory only to distinguish consecutive calls.
            if ($call.CallId -ne $lastCallId) {
                $callSequence++
                $lastCallId = $call.CallId
                $lastCallDirection = 'unknown'
            }
            if ($lastCallDirection -eq 'unknown') {
                # CallDirection is immutable. Read only this property, never
                # enumerate/serialize PhoneCallInfo (it also contains personal data).
                try {
                    $lastCallDirection = switch ($call.GetPhoneCallInfo().CallDirection.ToString()) {
                        'Outgoing' { 'outgoing' }
                        'Incoming' { 'incoming' }
                        default { 'unknown' }
                    }
                } catch { $lastCallDirection = 'unknown' }
            }
            $state.callDirection = $lastCallDirection
            $state.callSequence = $callSequence
            $state.callPhase = switch ($call.Status.ToString()) {
                'Talking' { 'connected' }
                'Dialing' { 'dialing' }
                'Incoming' { 'incoming' }
                'Held' { 'held' }
                'Ended' { 'ended' }
                'Lost' { 'lost' }
                default { 'unknown' }
            }
            $state.active = $state.callPhase -eq 'connected'
            $state.audioOnComputer = $call.AudioDevice.ToString() -eq 'LocalDevice'
        } else {
            # Multiple/held calls are deliberately not selected automatically.
            $state.callPhase = 'multiple'
        }
        Emit-State $state
    } catch {
        $store = $null
        # Avoid raw exception messages, which could contain device identifiers.
        $failedState = @{available=$false; active=$false; callPhase='unknown'; callDirection='unknown'; callCount=0; callSequence=$callSequence; errorCode='query-failed'}
        if ($Diagnostic) { $failedState.errorStage = $probeStage; $failedState.errorReason = $probeReason; $failedState.errorHresult = $_.Exception.HResult }
        Emit-State $failedState
    }
    $remaining = $IntervalMs - [int]$started.ElapsedMilliseconds
    if (-not $Once -and $remaining -gt 0) { Start-Sleep -Milliseconds $remaining }
} while (-not $Once)
