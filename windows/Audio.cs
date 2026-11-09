using NAudio.CoreAudioApi;
using NAudio.Wave;
using NAudio.Wave.SampleProviders;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Channels;

namespace GaevAssistant;
[ComImport,Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")] class PolicyClient {}
[ComImport,Guid("F8679F50-850A-41CF-9C72-430F290290C8"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicy {
 void GetMixFormat();void GetDeviceFormat();void ResetDeviceFormat();void SetDeviceFormat();void GetProcessingPeriod();void SetProcessingPeriod();void GetShareMode();void SetShareMode();void GetPropertyValue();void SetPropertyValue();
 [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id,int role);
 void SetEndpointVisibility();
}
sealed class MicLease:IDisposable {
 record Saved(string Target,string[] Original);
 static readonly Role[] Roles=[Role.Console,Role.Multimedia,Role.Communications];
 static void Set(string id,Role role){var p=(IPolicy)new PolicyClient();try{Marshal.ThrowExceptionForHR(p.SetDefaultEndpoint(id,(int)role));}finally{Marshal.ReleaseComObject(p);}}
 public static void Recover(){var path=Data.FileAt("microphone-restore.json");if(!File.Exists(path))return;var s=JsonSerializer.Deserialize<Saved>(File.ReadAllText(path))!;using var en=new MMDeviceEnumerator();for(int i=0;i<3;i++){using var d=en.GetDefaultAudioEndpoint(DataFlow.Capture,Roles[i]);if(d.ID==s.Target)Set(s.Original[i],Roles[i]);}File.Delete(path);}
 public static MMDevice Cable(DataFlow flow){using var en=new MMDeviceEnumerator();var prefix=flow==DataFlow.Render?"CABLE Input":"CABLE Output";var devices=en.EnumerateAudioEndPoints(flow,DeviceState.Active).Where(d=>d.FriendlyName.StartsWith(prefix)&&d.FriendlyName.Contains("VB-Audio")).ToArray();if(devices.Length!=1)throw new Exception("Установите VB-CABLE: не найдена единственная пара CABLE Input / Output.");return devices[0];}
 public MicLease(){Recover();using var en=new MMDeviceEnumerator();using var target=Cable(DataFlow.Capture);using var output=Cable(DataFlow.Render);using var speaker=en.GetDefaultAudioEndpoint(DataFlow.Render,Role.Multimedia);if(speaker.ID==output.ID)throw new Exception("Выберите обычные динамики Windows вместо CABLE Input.");var original=Roles.Select(r=>{using var d=en.GetDefaultAudioEndpoint(DataFlow.Capture,r);return d.ID;}).ToArray();File.WriteAllText(Data.FileAt("microphone-restore.json"),JsonSerializer.Serialize(new Saved(target.ID,original)));try{foreach(var r in Roles)Set(target.ID,r);}catch{Recover();throw;}}
 public void Dispose()=>Recover();
}
sealed class AudioBridge:IDisposable {
 readonly WasapiLoopbackCapture capture;readonly WasapiOut output;readonly BufferedWaveProvider input;readonly BufferedWaveProvider playback;readonly IWaveProvider converted;
 long captureBytes,sentFrames,maxPumpGap;
 public string Statistics=>"captureBytes="+Interlocked.Read(ref captureBytes)+", inputPackets="+Interlocked.Read(ref sentFrames)+", maxPumpIntervalMs="+Interlocked.Read(ref maxPumpGap)+", pendingInputBytes="+input.BufferedBytes+", pendingOutputBytes="+playback.BufferedBytes;
 readonly CancellationTokenSource pumpStop=new();Task? pump;
 readonly Channel<byte[]> frames=Channel.CreateBounded<byte[]>(new BoundedChannelOptions(30){FullMode=BoundedChannelFullMode.Wait,SingleReader=true,SingleWriter=true});
 public ChannelReader<byte[]> Frames=>frames.Reader;
 public int QueuedBytes=>playback.BufferedBytes;
 public AudioBridge(int inputRate,int outputRate){try{
  using var en=new MMDeviceEnumerator();using var speaker=en.GetDefaultAudioEndpoint(DataFlow.Render,Role.Multimedia);
  capture=new WasapiLoopbackCapture(speaker);input=new BufferedWaveProvider(capture.WaveFormat){BufferDuration=TimeSpan.FromSeconds(2),ReadFully=true};
  ISampleProvider samples=input.ToSampleProvider();if(samples.WaveFormat.Channels==2)samples=new StereoToMonoSampleProvider(samples){LeftVolume=.5f,RightVolume=.5f};else if(samples.WaveFormat.Channels!=1)throw new Exception("Для захвата звука выберите моно или стереодинамики.");
  converted=new SampleToWaveProvider16(new WdlResamplingSampleProvider(samples,inputRate));
  playback=new BufferedWaveProvider(new WaveFormat(outputRate,16,1)){BufferDuration=TimeSpan.FromSeconds(30),ReadFully=true};
  using var cable=MicLease.Cable(DataFlow.Render);output=new WasapiOut(cable,AudioClientShareMode.Shared,false,60);output.Init(playback);
  capture.DataAvailable+=(s,e)=>{try{input.AddSamples(e.Buffer,0,e.BytesRecorded);Interlocked.Add(ref captureBytes,e.BytesRecorded);}catch(Exception ex){frames.Writer.TryComplete(ex);}};
  pump=Task.Run(async()=>{try{var timer=System.Diagnostics.Stopwatch.StartNew();long previous=0;using var clock=new PeriodicTimer(TimeSpan.FromMilliseconds(100));while(await clock.WaitForNextTickAsync(pumpStop.Token)){var tick=timer.ElapsedMilliseconds;Interlocked.Exchange(ref maxPumpGap,Math.Max(maxPumpGap,tick-previous));previous=tick;Interlocked.Increment(ref sentFrames);var buffer=new byte[inputRate/10*2];int n=converted.Read(buffer,0,buffer.Length);if(n!=buffer.Length)throw new Exception("Не удалось подготовить PCM-пакет.");if(!frames.Writer.TryWrite(buffer))throw new Exception("Отправка звука отстаёт: переполнена очередь.");}}catch(OperationCanceledException){}catch(Exception ex){frames.Writer.TryComplete(ex);}});
  capture.RecordingStopped+=(s,e)=>frames.Writer.TryComplete(e.Exception);output.Play();capture.StartRecording();
 }catch{pumpStop.Cancel();pump?.GetAwaiter().GetResult();capture?.Dispose();output?.Dispose();pumpStop.Dispose();throw;}
 }
 public void Play(byte[] bytes){if(bytes.Length%2!=0)throw new Exception("Неполный PCM-пакет.");playback.AddSamples(bytes,0,bytes.Length);}
 public void Interrupt()=>playback.ClearBuffer();
 public void Dispose(){pumpStop.Cancel();pump?.GetAwaiter().GetResult();pumpStop.Dispose();try{capture.StopRecording();}finally{capture.Dispose();try{output.Stop();}finally{output.Dispose();frames.Writer.TryComplete();}}}
}
