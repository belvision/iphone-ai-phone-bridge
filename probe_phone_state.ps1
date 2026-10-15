# Windows telephony probe and guarded end command. Windows PowerShell 5.1.
# Normally read-only. -EndCurrentCall accepts one private token on stdin and
# can end only the matching single Talking call; never dials or changes routing.
# Current-call number/name go only to the parent monitor for local CRM matching.
# Line IDs and call IDs never leave this process; no address book is enumerated.
# Official API: https://learn.microsoft.com/en-us/uwp/api/windows.applicationmodel.calls.phonecallstatus
param(
    [ValidateRange(100, 5000)][int]$IntervalMs = 250,
    [switch]$Once,
    [switch]$Diagnostic,
    [switch]$EndCurrentCall
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

function Get-BoundedContactText([string]$Value, [int]$Limit) {
    $text = ($Value -replace '[\x00-\x1f\x7f-\x9f]', '').Trim()
    if ($text.Length -gt $Limit) {
        $text = $text.Substring(0, $Limit)
        if ([char]::IsHighSurrogate($text[$text.Length - 1])) { $text = $text.Substring(0, $text.Length - 1) }
    }
    return $text
}

function Get-CallToken([string]$CallId) {
    if ([string]::IsNullOrWhiteSpace($CallId)) { throw 'Call identifier unavailable' }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($CallId))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Emit-EndResult([string]$Status, [bool]$Attempted = $false) {
    [Console]::WriteLine((@{operation='end-call'; status=$Status; attempted=$Attempted} | ConvertTo-Json -Compress))
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [void][Windows.ApplicationModel.Calls.PhoneCallStore,Windows.ApplicationModel.Calls,ContentType=WindowsRuntime]
    [void][Windows.ApplicationModel.Calls.PhoneLine,Windows.ApplicationModel.Calls,ContentType=WindowsRuntime]
    [void][Windows.ApplicationModel.Calls.PhoneCallsResult,Windows.ApplicationModel.Calls,ContentType=WindowsRuntime]
    [void][Windows.ApplicationModel.Calls.PhoneCallOperationStatus,Windows.ApplicationModel.Calls,ContentType=WindowsRuntime]
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

if ($EndCurrentCall) {
    $attempted = $false
    try {
        # Token is transported over stdin, never command-line arguments or logs.
        $expectedToken = [Console]::In.ReadLine()
        if ($expectedToken -notmatch '^[a-f0-9]{64}$') { Emit-EndResult 'invalid-target'; exit 0 }
        $endStore = Await-WinRT ([Windows.ApplicationModel.Calls.PhoneCallManager]::RequestStoreAsync()) ([Windows.ApplicationModel.Calls.PhoneCallStore])
        $endLineId = Await-WinRT ($endStore.GetDefaultLineAsync()) ([Guid])
        $endLine = Await-WinRT ([Windows.ApplicationModel.Calls.PhoneLine]::FromIdAsync($endLineId)) ([Windows.ApplicationModel.Calls.PhoneLine])
        if (-not $endLine -or $endLine.Transport.ToString() -ne 'Bluetooth') { Emit-EndResult 'unavailable'; exit 0 }
        $endResult = Await-WinRT ($endLine.GetAllActivePhoneCallsAsync()) ([Windows.ApplicationModel.Calls.PhoneCallsResult])
        if ($endResult.OperationStatus.ToString() -ne 'Succeeded') { Emit-EndResult 'unavailable'; exit 0 }
        $endCalls = @(Get-NonterminalCalls $endResult.AllActivePhoneCalls)
        if ($endCalls.Count -eq 0) { Emit-EndResult 'not-connected'; exit 0 }
        if ($endCalls.Count -ne 1) { Emit-EndResult 'multiple-calls'; exit 0 }
        $endCall = $endCalls[0]
        if ((Get-CallToken $endCall.CallId) -cne $expectedToken) { Emit-EndResult 'wrong-call'; exit 0 }
        if ($endCall.Status.ToString() -ne 'Talking') { Emit-EndResult 'not-connected'; exit 0 }
        if ($endCall.AudioDevice.ToString() -ne 'LocalDevice') { Emit-EndResult 'not-on-computer'; exit 0 }
        $attempted = $true
        $operation = Await-WinRT ($endCall.EndAsync()) ([Windows.ApplicationModel.Calls.PhoneCallOperationStatus])
        $status = switch ($operation.ToString()) {
            'Succeeded' { 'succeeded' }
            'TimedOut' { 'timed-out' }
            'ConnectionLost' { 'connection-lost' }
            'InvalidCallState' { 'not-connected' }
            default { 'command-failed' }
        }
        Emit-EndResult $status $attempted
    } catch { Emit-EndResult 'command-failed' $attempted }
    exit 0
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
                # Read explicit properties only; never serialize PhoneCallInfo
                # itself (it also contains line IDs and other personal data).
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
            if ($state.active) {
                $state.phoneCallToken = Get-CallToken $call.CallId
                # User-authorized current caller metadata for local CRM. A
                # contact lookup failure must not stop call-state monitoring.
                try {
                    $callInfo = $call.GetPhoneCallInfo()
                    $state.phoneNumber = Get-BoundedContactText $callInfo.PhoneNumber 100
                    $state.displayName = Get-BoundedContactText $callInfo.DisplayName 200
                } catch {}
            }
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
