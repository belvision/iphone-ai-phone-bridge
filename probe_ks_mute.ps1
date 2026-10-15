# Diagnostic only. Sends silence to HFP; briefly mutes one existing call.
# Never dials, answers, transfers, hangs up, or connects to Retell.
# Run explicitly with: powershell -NoProfile -File .\probe_ks_mute.ps1 -RunLive
param([switch]$RunLive)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Emit-Probe([hashtable]$Value) {
    [Console]::WriteLine(($Value | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
}

if (-not $RunLive) {
    Emit-Probe @{status='prepared'; live=$false; durationPerPhaseSeconds=4; phases=@('baseline', 'muted')}
    exit 0
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Bridge Python is unavailable' }

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

function Await-WinRT($Operation, [Type]$ResultType) {
    $task = $script:asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait(1500)) {
        try { $Operation.Cancel() } catch {}
        throw 'WinRT query timeout'
    }
    return $task.Result
}

function Get-ActiveCalls($Line) {
    $result = Await-WinRT ($Line.GetAllActivePhoneCallsAsync()) ([Windows.ApplicationModel.Calls.PhoneCallsResult])
    if ($result.OperationStatus.ToString() -ne 'Succeeded') { throw 'Phone call query failed' }
    return @($result.AllActivePhoneCalls)
}

# No identifiers, caller information, microphone input, or raw audio are logged.
$pythonProbe = @'
import sounddevice as sd, time, statistics, json, sys
events, statuses = [], []
stream = None
try:
    apis = sd.query_hostapis()
    devices = [i for i, d in enumerate(sd.query_devices())
               if d['max_output_channels'] >= 1
               and 'Hands-Free HF Audio' in d['name']
               and 'WDM-KS' in apis[d['hostapi']]['name']]
    if len(devices) != 1:
        raise RuntimeError('hfp-device-count')
    def callback(data, frames, timing, status):
        data[:] = bytes(len(data))
        events.append((time.perf_counter(), frames))
        if status:
            statuses.append(str(status))
    stream = sd.RawOutputStream(device=devices[0], samplerate=16000,
                                channels=1, dtype='int16', blocksize=320,
                                latency='high', callback=callback)
    stream.start()
    begin = time.perf_counter()
    time.sleep(4)
    cpu, latency, active = stream.cpu_load, stream.latency, stream.active
    stream.abort()
    stream.close()
    stream = None
    steady = [entry for entry in events if entry[0] > begin + 0.5]
    span = steady[-1][0] - steady[0][0] if len(steady) > 1 else 0
    rate = sum(entry[1] for entry in steady[1:]) / span if span else 0
    gaps = [(steady[i][0] - steady[i-1][0]) * 1000 for i in range(1, len(steady))]
    print(json.dumps(dict(phase=sys.argv[1], status='measured',
        active=active, callbacks=len(events), steadyCallbacks=len(steady),
        frames=sorted(set(entry[1] for entry in events)), rate=round(rate, 2),
        latency=latency, cpu=cpu, callbackStatuses=len(statuses),
        medianMs=round(statistics.median(gaps), 4) if gaps else None,
        maxMs=round(max(gaps), 4) if gaps else None)), flush=True)
except Exception as exc:
    print(json.dumps(dict(phase=sys.argv[1], status='failed', errorType=type(exc).__name__)), flush=True)
    sys.exit(1)
finally:
    if stream is not None:
        try:
            stream.abort()
        finally:
            stream.close()
'@

$line = $null
$originalCallId = $null
$originalMuted = $false
$muteAttempted = $false
$failed = $false
try {
    $store = Await-WinRT ([Windows.ApplicationModel.Calls.PhoneCallManager]::RequestStoreAsync()) ([Windows.ApplicationModel.Calls.PhoneCallStore])
    $lineId = Await-WinRT ($store.GetDefaultLineAsync()) ([Guid])
    if ($lineId -eq [Guid]::Empty) { throw 'Default line unavailable' }
    $line = Await-WinRT ([Windows.ApplicationModel.Calls.PhoneLine]::FromIdAsync($lineId)) ([Windows.ApplicationModel.Calls.PhoneLine])
    if (-not $line -or $line.Transport.ToString() -ne 'Bluetooth') { throw 'Default line is not Bluetooth' }
    $calls = @(Get-ActiveCalls $line)
    if ($calls.Count -ne 1) { throw 'Expected exactly one active call' }
    $call = $calls[0]
    if ($call.Status.ToString() -ne 'Talking' -or $call.AudioDevice.ToString() -ne 'LocalDevice') {
        throw 'Call must be talking with audio on this computer'
    }
    $originalCallId = $call.CallId
    $originalMuted = [bool]$call.IsMuted
    Emit-Probe @{status='starting'; originalMuted=$originalMuted; durationPerPhaseSeconds=4}
    & $python -c $pythonProbe 'baseline'
    if ($LASTEXITCODE -ne 0) { throw 'Baseline audio probe failed' }

    $calls = @(Get-ActiveCalls $line)
    if ($calls.Count -ne 1 -or $calls[0].CallId -ne $originalCallId -or
        $calls[0].Status.ToString() -ne 'Talking' -or $calls[0].AudioDevice.ToString() -ne 'LocalDevice') {
        throw 'Original call changed before mute'
    }
    $call = $calls[0]
    $muteAttempted = $true
    $muteStatus = $call.Mute().ToString()
    Emit-Probe @{status='mute-result'; operationStatus=$muteStatus}
    if ($muteStatus -ne 'Succeeded') { throw 'Mute was not successful' }
    Start-Sleep -Milliseconds 200
    $calls = @(Get-ActiveCalls $line)
    if ($calls.Count -ne 1 -or $calls[0].CallId -ne $originalCallId -or
        $calls[0].Status.ToString() -ne 'Talking' -or -not $calls[0].IsMuted) {
        throw 'Mute state was not confirmed on original call'
    }
    & $python -c $pythonProbe 'muted'
    if ($LASTEXITCODE -ne 0) { throw 'Muted audio probe failed' }
} catch {
    $failed = $true
    Emit-Probe @{status='failed'; errorCode='probe-failed'}
} finally {
    if ($muteAttempted -and $line -and $originalCallId) {
        try {
            # Restore only the original call, never a subsequent or different call.
            $same = @(Get-ActiveCalls $line | Where-Object { $_.CallId -eq $originalCallId })
            if ($same.Count -eq 1 -and $same[0].Status.ToString() -notin @('Ended', 'Lost')) {
                $restoreStatus = 'Unchanged'
                if ([bool]$same[0].IsMuted -ne $originalMuted) {
                    $restoreStatus = if ($originalMuted) { $same[0].Mute().ToString() } else { $same[0].Unmute().ToString() }
                }
                Start-Sleep -Milliseconds 100
                $same = @(Get-ActiveCalls $line | Where-Object { $_.CallId -eq $originalCallId })
                $restored = $same.Count -eq 1 -and ([bool]$same[0].IsMuted -eq $originalMuted)
                Emit-Probe @{status='restore'; operationStatus=$restoreStatus; restored=$restored; targetMuted=$originalMuted}
                if (-not $restored) { $failed = $true }
            } else {
                Emit-Probe @{status='restore-skipped'; reason='original-call-no-longer-active'}
            }
        } catch {
            $failed = $true
            Emit-Probe @{status='restore-failed'; targetMuted=$originalMuted}
        }
    }
}
if ($failed) { exit 1 }
